import os
import re
import yaml
import json
import copy
import hashlib
import zipfile
import tarfile
import stat
import subprocess
import requests
import shutil
from urllib.parse import urljoin

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

MAX_ATTEMPTS = 5

PINNED = {"stable": "", "nightly": "", "beta": ""}

CHANNEL_PKG = {
    "stable": "com.brave.browser",
    "beta": "com.brave.browser_beta",
    "nightly": "com.brave.browser_nightly"
}

BUNDLES = []

ABI_QUALS = {"arm64_v8a", "armeabi_v7a", "armeabi", "x86", "x86_64", "mips"}
DPI_QUALS = {"ldpi", "mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "anydpi", "tvdpi"}

def parse_ver(tag):
    try:
        nums = re.findall(r"\d+", str(tag))[:3]
        return tuple(int(x) for x in nums)
    except Exception:
        return (0, 0, 0)

def get_latest_cli_jar():
    api_url = "https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest"
    release = requests.get(api_url).json()
    for asset in release.get("assets", []):
        if asset["name"].endswith("-all.jar"):
            with requests.get(asset["browser_download_url"], stream=True) as r:
                r.raise_for_status()
                with open("build/cli.jar", 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return asset["name"]
    raise Exception("Could not find CLI jar")

def download_file(url, dest):
    print(f"Downloading {url} ...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def download_file_ua(url, dest, timeout=900):
    print(f"Downloading (browser UA) {url} ...")
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"}
    with requests.get(url, stream=True, timeout=timeout, headers=hdr) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)

def is_zip(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except Exception:
        return False

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_apkeep():
    path = "build/apkeep"
    if os.path.exists(path):
        return path
    rel = requests.get("https://api.github.com/repos/EFForg/apkeep/releases/latest").json()
    names = [a["name"] for a in rel.get("assets", [])]
    print(f"apkeep release assets: {names}")
    asset = None
    for a in rel.get("assets", []):
        n = a["name"].lower()
        if "x86_64" in n and "linux" in n and not n.endswith((".deb", ".rpm")):
            asset = a
            break
    if asset is None:
        for a in rel.get("assets", []):
            n = a["name"].lower()
            if "linux" in n and not n.endswith((".deb", ".rpm")):
                asset = a
                break
    if asset is None:
        raise Exception(f"no linux apkeep asset found in {names}")
    raw = "build/apkeep_dl"
    download_file(asset["browser_download_url"], raw)
    if asset["name"].endswith(".tar.gz"):
        with tarfile.open(raw) as t:
            mem = None
            for m in t.getmembers():
                if m.isfile() and m.name.rstrip("/").split("/")[-1] == "apkeep":
                    mem = m
                    break
            if mem is None:
                mem = next(m for m in t.getmembers() if m.isfile())
            f = t.extractfile(mem)
            with open(path, "wb") as out:
                out.write(f.read())
    elif asset["name"].endswith(".zip"):
        with zipfile.ZipFile(raw) as z:
            znames = [n for n in z.namelist() if n.rstrip("/").split("/")[-1] == "apkeep"]
            data = z.read(znames[0] if znames else z.namelist()[0])
            with open(path, "wb") as out:
                out.write(data)
    else:
        shutil.move(raw, path)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path

def apkeep_download(apkeep, pkg, version, arch, outdir):
    os.makedirs(outdir, exist_ok=True)
    spec = f"{pkg}@{version}" if version else pkg
    attempts = [
        [apkeep, "-a", spec, "-d", "apk-pure", "-o", f"arch={arch}", outdir],
        [apkeep, "-a", spec, "-o", f"arch={arch}", outdir],
        [apkeep, "-a", spec, outdir],
    ]
    for cmd in attempts:
        print("Running:", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        print(r.stdout)
        if r.stderr:
            print(r.stderr)
        if r.returncode == 0:
            files = [os.path.join(outdir, f) for f in os.listdir(outdir)]
            files = [f for f in files if os.path.isfile(f) and is_zip(f)]
            if files:
                return max(files, key=os.path.getsize)
    return None

def select_splits(entries, arch, density, languages):
    arch_q = arch.replace("-", "_")
    keep = []
    for n in entries:
        b = os.path.basename(n).lower()
        if ".config." not in b:
            keep.append(n)
            continue
        qual = b.split(".config.")[-1].replace(".apk", "")
        if qual in ABI_QUALS:
            if qual == arch_q:
                keep.append(n)
        elif qual in DPI_QUALS:
            if qual == density:
                keep.append(n)
        elif qual.isalpha() and len(qual) <= 3:
            if qual in languages:
                keep.append(n)
        else:
            keep.append(n)
    return keep

def get_releases(repo):
    return requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=100").json()

def get_latest_stable(repo):
    return requests.get(f"https://api.github.com/repos/{repo}/releases/latest").json()

def get_release_status(repo, version):
    if not repo:
        return "unknown"
    try:
        for r in get_releases(repo):
            t = (r.get("tag_name") or "").lower().lstrip("v")
            if t == str(version).lower().lstrip("v"):
                return "prerelease" if r.get("prerelease") else "stable"
    except Exception:
        pass
    return "unknown"

def repo_from_url(url):
    m = re.search(r"raw\.githubusercontent\.com/([^/]+/[^/]+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"bundle/([^/]+/[^/]+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"github\.com/([^/]+/[^/]+)/", url)
    if m:
        return m.group(1)
    return None

def cf_get(url, timeout=20):
    if HAS_CURL_CFFI:
        for imp in ["chrome136", "chrome133", "chrome131", "chrome124", "chrome120", "chrome110"]:
            try:
                s = cffi_requests.Session(impersonate=imp)
                r = s.get(url, timeout=timeout, allow_redirects=True)
                low = r.text[:600].lower()
                if r.status_code == 200 and not any(p in low for p in ("just a moment", "attention required", "turnstile", "verify you are human")):
                    return r.text
            except Exception:
                continue
    try:
        hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"}
        r = requests.get(url, timeout=timeout, headers=hdr)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None

def scrape_apkpure_net(spec, version, arch):
    if not HAS_BS4:
        print("scraper: beautifulsoup4 missing, skip apkpure.net")
        return None
    pkg = spec.get("package", "")
    names = [spec.get("apkpure_name", ""), pkg.split(".")[-1], pkg.replace(".", "-")]
    for nm in [n for n in names if n]:
        try:
            url = f"https://apkpure.net/{nm}/{pkg}/download/{version}" if version else f"https://apkpure.net/{nm}/{pkg}"
            print(f"scraper apkpure.net: {url}")
            html = cf_get(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            link = soup.find("a", id="download_link")
            if not link or not link.get("href"):
                continue
            href = link["href"]
            if href.startswith("http"):
                return href
        except Exception as e:
            print(f"scraper apkpure.net failed: {e}")
    return None

def scrape_uptodown(spec, version, arch):
    if not HAS_BS4:
        print("scraper: beautifulsoup4 missing, skip uptodown")
        return None
    pkg = spec.get("package", "")
    slugs = [spec.get("uptodown_slug", ""), pkg.split(".")[-1], pkg.replace(".", "-")]
    slugs = [s for s in slugs if s]
    for slug in slugs:
        for locale in ("en", "de", "fr", "in", "it", "ru", "jp", "kr"):
            base = f"https://{slug}.{locale}.uptodown.com/android"
            try:
                html = cf_get(base)
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                h1 = soup.find("h1", id="detail-app-name")
                if not h1:
                    continue
                data_code = h1.get("data-code")
                if not data_code:
                    continue
                found = False
                for page in range(1, 6):
                    pj = cf_get(f"{base}/apps/{data_code}/versions/{page}")
                    if not pj:
                        break
                    try:
                        entries = (json.loads(pj) or {}).get("data") or []
                    except Exception:
                        break
                    if not entries:
                        break
                    for entry in entries:
                        ev = entry.get("version", "")
                        if version and ev != version:
                            continue
                        parts = entry.get("versionURL") or {}
                        vu = "/".join(str(parts.get(k, "")).strip("/") for k in ("url", "extraURL", "versionID"))
                        if not vu:
                            continue
                        vhtml = cf_get(vu if vu.startswith("http") else urljoin(base, vu))
                        if not vhtml:
                            continue
                        vsoup = BeautifulSoup(vhtml, "html.parser")
                        vbtn = vsoup.select_one(".button.variants[data-version]")
                        file_id = None
                        if vbtn:
                            data_version = vbtn.get("data-version")
                            cat = f"{base.rsplit('/android', 1)[0]}/app/{data_code}/version/{data_version}/files"
                            cj = cf_get(cat)
                            if cj:
                                try:
                                    content = (json.loads(cj) or {}).get("content") or ""
                                except Exception:
                                    content = ""
                                if content:
                                    csoup = BeautifulSoup(content, "html.parser")
                                    cur_arch = ""
                                    for node in csoup.select("section.variants > .content > *"):
                                        if node.name == "p":
                                            cur_arch = node.get_text(" ", strip=True).lower()
                                            continue
                                        rep = node.select_one(".v-report[data-file-id]") if node.name != "p" else None
                                        if not rep:
                                            continue
                                        fid = rep.get("data-file-id")
                                        if arch.replace("-", "_").replace("_", "-") in cur_arch or arch in cur_arch:
                                            file_id = fid
                                            break
                                        if not file_id:
                                            file_id = fid
                        if file_id:
                            dx = cf_get(f"{base}/download/{file_id}-x")
                            if dx:
                                dsoup = BeautifulSoup(dx, "html.parser")
                                btn = dsoup.find(id="detail-download-button")
                                if btn and btn.get("data-url"):
                                    return urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                        btn = vsoup.find(id="detail-download-button")
                        if btn and btn.get("data-url"):
                            return urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                        found = True
                        break
                    if found:
                        break
            except Exception as e:
                print(f"scraper uptodown failed ({slug}/{locale}): {e}")
    return None

def scrape_apkmirror(spec, version, arch, density):
    base = "https://www.apkmirror.com"
    org = spec.get("apkmirror_org", "")
    name = spec.get("apkmirror_name", "")
    btype = (spec.get("apkmirror_type") or "APK").upper()
    if not org or not name:
        return None
    try:
        app_html = cf_get(f"{base}/apk/{org}/{name}/")
        if not app_html:
            return None
        ver_slug = version.replace(".", "-")
        links = re.findall(r'href="(/apk/[^"]+?%s[^"]*/)"' % re.escape(ver_slug), app_html)
        seen = set()
        rels = []
        for l in links:
            if l not in seen:
                seen.add(l)
                rels.append(l)
        print(f"scraper apkmirror: {len(rels)} candidate release pages for {version}")
        for rel in rels[:8]:
            page = cf_get(base + rel)
            if not page:
                continue
            rows = re.split(r'<div class="[^"]*table-row[^"]*headerFont[^"]*"[^>]*>', page)[1:]
            for r in rows:
                badge = re.search(r'apkm-badge[^"]*"[^>]*>([^<]+)</span>', r)
                node_type = badge.group(1).strip().upper() if badge else "APK"
                if node_type != btype:
                    continue
                cells = re.findall(r'<div class="table-cell[^"]*"[^>]*>(.*?)</div>', r, re.S)
                node_arch = re.sub(r'<[^>]+>', '', cells[1]).strip().lower() if len(cells) > 1 else ""
                node_dpi = re.sub(r'<[^>]+>', '', cells[3]).strip().lower() if len(cells) > 3 else ""
                if arch not in node_arch and not ("arm64" in node_arch and arch == "arm64-v8a"):
                    continue
                if density and density not in node_dpi and "nodpi" not in node_dpi and "anydpi" not in node_dpi:
                    continue
                href_m = re.search(r'href="((?:https://www\.apkmirror\.com)?/apk/[^"]+)"', r)
                if not href_m:
                    continue
                vurl = href_m.group(1)
                if not vurl.startswith("http"):
                    vurl = base + vurl
                vpage = cf_get(vurl)
                if not vpage:
                    continue
                m = re.search(r'class="[^"]*downloadButton[^"]*"[^>]*href="([^"]+)"', vpage) or re.search(r'href="([^"]+)"[^>]*class="[^"]*downloadButton', vpage)
                if not m:
                    continue
                dpage = cf_get(base + m.group(1))
                if not dpage:
                    continue
                m2 = re.search(r'id="download-link"[^>]*href="([^"]+)"', dpage) or re.search(r'href="([^"]+)"[^>]*id="download-link"', dpage)
                if not m2:
                    continue
                return base + m2.group(1)
    except Exception as e:
        print(f"scraper apkmirror failed: {e}")
    return None

def find_uploaded_asset(own_repo, up_tag, aid, pkg):
    candidates = []
    r = requests.get(f"https://api.github.com/repos/{own_repo}/releases/tags/{up_tag}")
    if r.status_code == 200:
        candidates.append(r.json())
    r = requests.get(f"https://api.github.com/repos/{own_repo}/releases/latest")
    if r.status_code == 200:
        candidates.append(r.json())
    seen = set()
    uniq = []
    for rel in candidates:
        if isinstance(rel, dict) and rel.get("id") not in seen:
            seen.add(rel.get("id"))
            uniq.append(rel)
    ids = [x for x in (aid.lower(), (pkg or "").lower()) if x]

    def acceptable(n):
        n = n.lower()
        if "patched" in n:
            return False
        if not any(i in n for i in ids):
            return False
        return n.endswith((".apkm", ".xapk", ".zip", ".apk"))

    for rel in uniq:
        for a in rel.get("assets", []):
            if acceptable(a["name"]):
                return a, rel.get("tag_name")
    return None, None

def download_monochrome_from_kveld9(dest):
    try:
        rel = requests.get("https://api.github.com/repos/kveld9/kveld-morphe-patches/releases/latest").json()
        for a in rel.get("assets", []):
            if "mono" in a["name"].lower() and a["name"].lower().endswith(".apk"):
                download_file(a["browser_download_url"], dest)
                return rel.get("tag_name", "kveld9-mono")
    except Exception as e:
        print(f"kveld9 monochrome download failed: {e}")
    return None

def find_asset(release):
    for a in release.get("assets", []):
        n = a["name"].lower()
        if "arm64" in n and "universal" in n:
            return a["browser_download_url"]
    return None

def classify(r, stable_tag):
    name = (r.get("name") or "").strip().lower()
    tag = r.get("tag_name") or ""
    if tag == stable_tag or name.startswith("release"):
        return "stable"
    if name.startswith("nightly"):
        return "nightly"
    if name.startswith("beta"):
        return "beta"
    return None

def pick_candidates(releases, channel, latest_stable):
    stable_tag = latest_stable.get("tag_name") if latest_stable else None
    stable_ver = parse_ver(stable_tag)
    stable_cand = None
    if latest_stable:
        url = find_asset(latest_stable)
        if url:
            stable_cand = (stable_tag, url)
    old_stables, betas, nightlies, older = [], [], [], []
    for r in releases:
        tag = r.get("tag_name") or ""
        if tag == stable_tag:
            continue
        url = find_asset(r)
        if not url:
            continue
        entry = (tag, url)
        c = classify(r, stable_tag)
        if c == "stable":
            old_stables.append(entry)
        elif c == "beta":
            betas.append(entry)
        elif c == "nightly":
            nightlies.append(entry)
        if parse_ver(tag) < stable_ver:
            older.append(entry)
    if channel == "stable":
        cands = ([stable_cand] if stable_cand else []) + old_stables + older
    elif channel == "beta":
        cands = betas + ([stable_cand] if stable_cand else []) + older
    else:
        cands = nightlies + betas + ([stable_cand] if stable_cand else []) + older
    seen = set()
    out = []
    for t, u in cands:
        if t not in seen:
            seen.add(t)
            out.append((t, u))
    out = out[:MAX_ATTEMPTS]
    if stable_cand and stable_cand[0] not in [t for t, _ in out]:
        out.append(stable_cand)
    return out

def generate_options_file(bundles, out_path="build/gen_options.json"):
    for sub in ("options", "options-create"):
        cmd = ["java", "-jar", "build/cli.jar", sub]
        for b in bundles:
            cmd += ["-p", b]
        cmd += ["-o", out_path]
        r = subprocess.run(cmd)
        if r.returncode == 0 and os.path.exists(out_path):
            with open(out_path) as f:
                content = f.read()
            try:
                return json.loads(content)
            except Exception:
                return None
    return None

def set_option_value(opts, key, value):
    cur = opts.get(key)
    if isinstance(cur, dict):
        cur["value"] = value
    else:
        opts[key] = value

def norm_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def apply_option(entry, key, value):
    opts = entry.setdefault("options", {})
    nk = norm_key(key)
    if key in opts:
        set_option_value(opts, key, value)
        return
    for k in list(opts):
        if norm_key(k) == nk:
            set_option_value(opts, k, value)
            return
    for k in list(opts):
        ok = norm_key(k)
        if ok and (nk in ok or ok in nk):
            set_option_value(opts, k, value)
            return
    if len(opts) == 1:
        set_option_value(opts, list(opts)[0], value)
        return
    set_option_value(opts, key, value)

def enable_entry(entry, vals):
    entry["enabled"] = True
    applied = {}
    for k, v in vals.items():
        apply_option(entry, k, v)
        applied[norm_key(k)] = v
    pkg = applied.get("packagename")
    if not pkg:
        return
    for k in list((entry.get("options") or {})):
        kl = k.lower()
        raw = entry["options"][k]
        cur = raw.get("value") if isinstance(raw, dict) else raw
        if isinstance(cur, bool):
            if "update" in kl:
                set_option_value(entry["options"], k, True)
        elif cur is None:
            if "update" in kl:
                set_option_value(entry["options"], k, True)
            elif "package" in kl:
                set_option_value(entry["options"], k, pkg)
        else:
            if "package" in kl and "update" not in kl:
                set_option_value(entry["options"], k, pkg)

def make_variant_options(gen_data, wanted, per_bundle=None):
    data = copy.deepcopy(gen_data)
    found = set()
    if per_bundle is not None:
        for i, bundle in enumerate(data):
            w = per_bundle[i] if i < len(per_bundle) else {}
            for name, entry in (bundle.get("patches") or {}).items():
                if name in w:
                    found.add(name)
                    enable_entry(entry, w[name])
                else:
                    entry["enabled"] = False
        allw = set()
        for d in per_bundle:
            allw |= set(d)
        missing = [n for n in sorted(allw) if n not in found]
        return data, missing
    owners = {}
    for name in wanted:
        idxs = [j for j, b in enumerate(data) if name in (b.get("patches") or {})]
        if idxs:
            owners[name] = idxs[-1] if name.lower() == "clone app" else idxs[0]
    for i, bundle in enumerate(data):
        for name, entry in (bundle.get("patches") or {}).items():
            if name in wanted and owners.get(name) == i:
                found.add(name)
                enable_entry(entry, wanted[name])
            else:
                entry["enabled"] = False
    missing = [n for n in sorted(wanted) if n not in found]
    return data, missing

def needs_value_names(gen_data):
    need = set()
    for bundle in gen_data or []:
        for name, entry in (bundle.get("patches") or {}).items():
            for v in (entry.get("options") or {}).values():
                if v is None or (isinstance(v, dict) and v.get("value") is None):
                    need.add(name)
    return need

def detect_alias():
    ks = "signing/keystore.jks"
    pw = os.environ.get("KEYSTORE_PASSWORD", "")
    preferred = os.environ.get("KEY_ALIAS", "")
    try:
        out = subprocess.run(["keytool", "-list", "-keystore", ks, "-storepass", pw],
                             capture_output=True, text=True)
        aliases = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if "PrivateKeyEntry" in line or "trustedCertEntry" in line:
                alias = line.split(",")[0].strip()
                if alias:
                    aliases.append(alias)
        print(f"Aliases found in keystore: {aliases}")
        if preferred in aliases:
            return preferred
        if aliases:
            print(f"NOTE: KEY_ALIAS secret not in keystore, using detected alias: {aliases[0]}")
            return aliases[0]
    except Exception as e:
        print("alias detection failed:", e)
    return preferred

def keystore_fingerprint(alias):
    ks = "signing/keystore.jks"
    pw = os.environ.get("KEYSTORE_PASSWORD", "")
    r = subprocess.run(["keytool", "-list", "-v", "-keystore", ks, "-storepass", pw, "-alias", alias],
                       capture_output=True, text=True)
    m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
    return m.group(1).upper() if m else None

def apk_fingerprint(path):
    r = subprocess.run(["keytool", "-printcert", "-jarfile", path], capture_output=True, text=True)
    m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
    return m.group(1).upper() if m else None

def verify_signature(path, ks_fp):
    fp = apk_fingerprint(path)
    if fp is None:
        print(f"signature check: no v1 certificate readable in {path}, skipping compare")
        return None, True
    match = (fp == ks_fp) if ks_fp else None
    print(f"signature check: apk={fp} keystore={ks_fp} match={match}")
    if match is False:
        print(f"WARNING: signature differs from keystore: {path}")
    return fp, match

def parse_patches_info(bundles):
    cmd = ["java", "-jar", "build/cli.jar", "list-patches"]
    for b in bundles:
        cmd += ["-p", b]
    cmd += ["--with-packages", "--with-options"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout)
    info = []
    cur = None
    pending_required = False
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Index:"):
            cur = {"name": None, "packages": [], "required_opts": [], "last_key": None,
                   "pkg_versions": {}, "last_pkg": None}
            info.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Name:"):
            cur["name"] = line[5:].strip()
        elif line.startswith("Required:"):
            req = line.split(":", 1)[1].strip().lower() == "true"
            if req and cur.get("last_key"):
                cur["required_opts"].append(cur["last_key"])
            pending_required = req
        elif line.startswith("Key:"):
            key = line[4:].strip()
            cur["last_key"] = key
            if pending_required:
                cur["required_opts"].append(key)
            pending_required = False
        elif line.startswith("Package name:"):
            pkg = line.split(":", 1)[1].strip()
            cur["packages"].append(pkg)
            cur["last_pkg"] = pkg
            cur["pkg_versions"].setdefault(pkg, [])
        elif line.startswith("Recommended version:") or line.startswith("Suggested version:"):
            ver = line.split(":", 1)[1].strip()
            pkg = cur.get("last_pkg")
            if pkg:
                cur["pkg_versions"].setdefault(pkg, []).append(ver)
    return [p for p in info if p["name"]]

def recommended_versions(info, pkg):
    vers = []
    for p in info or []:
        vers.extend((p.get("pkg_versions") or {}).get(pkg, []))
    return vers

def compute_auto(info, channel_pkg, exclude, configured, needs_value):
    auto = []
    for p in info:
        n = p["name"]
        if n in configured or n in exclude:
            continue
        if p["required_opts"] or n in needs_value:
            continue
        if p["packages"] and channel_pkg in p["packages"]:
            auto.append(n)
    return auto

def run_patch(apk_path, out_apk, wanted, gen_data, label, alias, bundles=None, per_bundle=None):
    bundles = bundles if bundles is not None else BUNDLES
    cmd = ["java", "-jar", "build/cli.jar", "patch"]
    for b in bundles:
        cmd += ["-p", b]
    missing = []
    if gen_data is not None:
        data, missing = make_variant_options(gen_data, wanted, per_bundle)
        opts_path = f"build/options_{label}.json"
        with open(opts_path, "w") as f:
            json.dump(data, f, indent=2)
        with open(opts_path) as f:
            print(f"OPTIONS FILE FOR {label}:")
            print(f.read())
        cmd += ["--options-file", opts_path]
    else:
        for n, opts in wanted.items():
            cmd += ["-e", n]
            for k, v in opts.items():
                cmd += [f"-O{k}={v}"]
    if missing:
        print(f"NOTE: not present in bundle (skipped): {missing}")
    ks = "signing/keystore.jks"
    if os.path.exists(ks):
        cmd += ["--keystore", ks,
                "--keystore-password", os.environ.get("KEYSTORE_PASSWORD", ""),
                "--keystore-entry-alias", alias,
                "--keystore-entry-password", os.environ.get("KEY_PASSWORD", "")]
    cmd += ["--striplibs", "arm64-v8a"]
    cmd += ["-o", out_apk, "--continue-on-error", apk_path]
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout)
    if r.stderr:
        print(r.stderr)
    applied = []
    for m in re.findall(r"Applied:\s*(.+)", r.stdout):
        m = m.strip()
        if m not in applied:
            applied.append(m)
    failed = []
    for m in re.findall(r"FAILED:\s*(.+)", r.stdout + "\n" + r.stderr):
        m = m.strip()
        if m not in failed:
            failed.append(m)
    return r.returncode == 0, applied, failed, missing

def heal_patch(apk, out_apk, wanted, auto, gen_data, alias, label, bundles=None, per_bundle=None):
    auto = list(auto)
    dropped = []
    wanted = dict(wanted)
    pb = [dict(d) for d in per_bundle] if per_bundle else None
    while True:
        full = dict(wanted)
        for n in auto:
            if n not in full:
                full[n] = {}
        ok, applied, failed, missing = run_patch(apk, out_apk, full, gen_data, label, alias, bundles, pb)
        if ok:
            return True, applied, dropped
        auto_failed = [n for n in failed if n in auto]
        if not auto_failed:
            return False, applied, dropped
        for n in auto_failed:
            auto.remove(n)
            dropped.append(n)
            wanted.pop(n, None)
            if pb:
                for d in pb:
                    d.pop(n, None)

def get_apk(tag, url, cache):
    if tag not in cache:
        path = f"build/base_{tag.replace('.', '_')}.apk"
        download_file(url, path)
        cache[tag] = path
    return cache[tag]

def find_version(cands, wanted, auto_all, gen_data, alias, cache, label, start_tag=None, bundles=None):
    ordered = cands
    if start_tag:
        ordered = [c for c in cands if c[0] == start_tag] + [c for c in cands if c[0] != start_tag]
    for tag, url in ordered:
        apk = get_apk(tag, url, cache)
        ok, applied, dropped = heal_patch(apk, f"build/out_{label}_{tag.replace('.', '_')}.apk",
                                          wanted, auto_all, gen_data, alias, f"{label}_{tag}", bundles=bundles)
        if ok:
            print(f"{label}: working version -> {tag}")
            return tag, applied, dropped, False
        print(f"{label}: {tag} not workable, trying older...")
    tag, url = ordered[0]
    apk = get_apk(tag, url, cache)
    ok, applied, dropped = heal_patch(apk, f"build/out_{label}_{tag.replace('.', '_')}.apk",
                                      wanted, auto_all, gen_data, alias, f"{label}_besteffort", bundles=bundles)
    print(f"{label}: no fully working version, best effort on {tag}")
    return tag, applied, dropped, True

def download_bundle_from_json(url, dest):
    j = requests.get(url).json()
    ver = j.get("version", "unknown")
    dl = j.get("download_url")
    if not dl:
        raise Exception(f"bundle json has no download_url: {url}")
    download_file(dl, dest)
    return ver

def download_github_release_apk(spec, dest):
    repo = spec["repo"]
    tag = spec["tag"]
    match = (spec.get("match") or "").lower()
    rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}").json()
    assets = rel.get("assets", [])
    print(f"stock release assets: {[a['name'] for a in assets]}")
    chosen = None
    if match:
        hits = [a for a in assets if match in a["name"].lower()]
        if hits:
            chosen = hits[0]
    if chosen is None and len(assets) == 1:
        chosen = assets[0]
    if chosen is None:
        hits = [a for a in assets if "arm64" in a["name"].lower()]
        if hits:
            chosen = hits[0]
    if chosen is None:
        raise Exception(f"No matching APK asset in {repo}@{tag}")
    download_file(chosen["browser_download_url"], dest)
    return chosen["name"]

def save_scraped(raw, apk_path, arch, density, languages):
    if not (raw and os.path.exists(raw) and is_zip(raw)):
        return None
    with zipfile.ZipFile(raw) as z:
        entries = [n for n in z.namelist() if n.lower().endswith(".apk")]
    if entries:
        keep = select_splits(entries, arch, density, languages)
        if keep:
            with zipfile.ZipFile(apk_path, "w", zipfile.ZIP_DEFLATED) as zo:
                for n in keep:
                    zo.writestr(os.path.basename(n), z.read(n))
            return f"split subset ({arch}, {density}, {'/'.join(languages)})"
        return None
    if "AndroidManifest.xml" in zipfile.ZipFile(raw).namelist():
        p = f"build/base_single_{os.path.basename(apk_path)}"
        shutil.copyfile(raw, p)
        return f"single apk ({arch})"
    return None

def acquire_base(app, aid, appver, arch, density, languages):
    spec = app["apk"]
    apk_path = f"build/base_{aid}.apkm"
    source = spec.get("source", "apkeep")

    if source == "apkeep":
        try:
            apkeep = ensure_apkeep()
            bundle = apkeep_download(apkeep, spec.get("package", ""), appver, arch, f"build/apkeep_{aid}")
            if bundle:
                mode = save_scraped(bundle, apk_path, arch, density, languages)
                if mode:
                    return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", mode, True
        except Exception as e:
            print(f"{aid}: apkeep failed: {e}")
        vc = str(spec.get("version_code") or "").strip()
        if spec.get("package") and vc:
            for host in ("d.apkpure.net", "d.apkpure.com"):
                url = f"https://{host}/b/XAPK/{spec['package']}?versionCode={vc}"
                try:
                    print(f"Trying direct bundle: {url}")
                    raw = f"build/raw_{aid}.bin"
                    hdr = {"User-Agent": "Mozilla/5.0"}
                    with requests.get(url, stream=True, timeout=600, headers=hdr) as r:
                        if r.status_code == 200:
                            with open(raw, "wb") as f:
                                for chunk in r.iter_content(1 << 20):
                                    f.write(chunk)
                            mode = save_scraped(raw, apk_path, arch, density, languages)
                            if mode:
                                return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", mode, True
                except Exception as e:
                    print(f"direct bundle failed: {e}")

    if source in ("apkeep", "scraper"):
        print(f"{aid}: trying web scrapers for {spec.get('package')} {appver}")
        raw = f"build/scraper_{aid}.bin"
        dl = scrape_apkpure_net(spec, appver, arch)
        if dl:
            try:
                download_file_ua(dl, raw)
                mode = save_scraped(raw, apk_path, arch, density, languages)
                if mode:
                    return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", "apkpure.net " + mode, True
            except Exception as e:
                print(f"apkpure.net download failed: {e}")
        dl = scrape_uptodown(spec, appver, arch)
        if dl:
            try:
                download_file_ua(dl, raw)
                mode = save_scraped(raw, apk_path, arch, density, languages)
                if mode:
                    return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", "uptodown " + mode, True
            except Exception as e:
                print(f"uptodown download failed: {e}")
        dl = scrape_apkmirror(spec, appver, arch, density)
        if dl:
            try:
                download_file_ua(dl, raw)
                mode = save_scraped(raw, apk_path, arch, density, languages)
                if mode:
                    return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", "apkmirror " + mode, True
            except Exception as e:
                print(f"apkmirror download failed: {e}")

    up_tag = (spec.get("upload_tag") or "").strip()
    own_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if up_tag and own_repo:
        asset, from_tag = find_uploaded_asset(own_repo, up_tag, aid, spec.get("package", ""))
        if asset:
            print(f"uploaded apk asset: {asset['name']} from release tag {from_tag}")
            try:
                raw = f"build/upload_{aid}.bin"
                download_file(asset["browser_download_url"], raw)
                mode = save_scraped(raw, apk_path, arch, density, languages)
                if mode:
                    return apk_path if mode.startswith("split") else f"build/base_single_{aid}.apk", "uploaded " + mode, True
            except Exception as e:
                print(f"uploaded bundle layer failed: {e}")

    if spec.get("repo") and spec.get("tag"):
        try:
            p = f"build/base_{aid}.apk"
            download_github_release_apk(spec, p)
            return p, "universal fallback (full apk + striplibs)", True
        except Exception as e:
            print(f"{aid}: fallback apk failed: {e}")

    return None, "", False

def build_extra_app(app, alias, ks_fp, release_notes):
    aid = app["id"]
    print(f"\n=== Extra app: {aid} ===")
    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    density = (app.get("density") or "xxhdpi").strip()
    languages = [l.lower() for l in (app.get("languages") or ["en"])]
    variants = app.get("variants", [])

    configured_version = (spec.get("version") or "").strip()
    appver = configured_version

    for variant in variants:
        mpps = []
        okb = True
        for b in variant["bundles"]:
            mpp = f"bundles/{aid}_{b['label']}.mpp"
            try:
                ver = download_bundle_from_json(b["url"], mpp)
            except Exception as e:
                print(f"{variant['id']}: bundle {b['label']} failed: {e}")
                okb = False
                break
            b["_ver"] = ver
            b["_status"] = get_release_status(repo_from_url(b["url"]), ver)
            mpps.append(mpp)
        variant["_mpps"] = mpps
        variant["_ok"] = okb
        if not okb:
            continue
        info = parse_patches_info(mpps)
        variant["_info"] = info
        if configured_version in ("", "auto"):
            rec = recommended_versions(info, spec.get("package", ""))
            if rec:
                best = max(rec, key=parse_ver)
                if appver in ("", "auto"):
                    appver = best
                    print(f"{aid}: resolved recommended version from bundle: {appver}")
    if appver in ("", "auto", None):
        appver = "latest"
    for variant in variants:
        variant["_ver_resolved"] = appver
    print(f"{aid}: app version for this run: {appver}")

    apk_path, mode, got = acquire_base(app, aid, "" if appver == "latest" else appver, arch, density, languages)
    if not got:
        release_notes.append(f"## {aid}\nStatus: Failed (apk source)\n\n")
        return

    for variant in variants:
        vid = variant["id"]
        if not variant.get("_ok"):
            release_notes.append(f"## {vid}\nStatus: Failed (bundle download)\n\n")
            continue
        mpps = variant["_mpps"]
        gen = generate_options_file(mpps, out_path=f"build/gen_{vid}.json")
        if gen is None:
            release_notes.append(f"## {vid}\nStatus: Failed (options generation)\n\n")
            continue

        names_per = [set((g.get("patches") or {}).keys()) for g in gen]
        needs_per = [needs_value_names([g]) for g in gen]
        exclusive = bool(variant.get("merge_exclusive"))
        per_bundle = []
        lowers_seen = set()
        dup_skipped = []
        bundles_cfg = variant.get("bundles", [])
        for i in range(len(gen)):
            w = {}
            bundle_cfg = bundles_cfg[i] if i < len(bundles_cfg) else {}
            allowed = bundle_cfg.get("patches")
            allowed_lower = {p.lower() for p in allowed} if allowed is not None else None
            for n in sorted(names_per[i] - needs_per[i]):
                if allowed_lower is not None and n.lower() not in allowed_lower:
                    continue
                if exclusive and i > 0 and n.lower() in lowers_seen:
                    dup_skipped.append(n)
                    continue
                w[n] = {}
                lowers_seen.add(n.lower())
            per_bundle.append(w)

        excl = [e.strip().lower() for e in (variant.get("exclude_patches") or [])]
        if excl:
            for d in per_bundle:
                for n in list(d):
                    if n.lower() in excl:
                        del d[n]

        for pname, opts in (variant.get("options") or {}).items():
            pl = pname.strip().lower()
            for i, bundle in enumerate(gen):
                for n in (bundle.get("patches") or {}):
                    if n.lower() == pl:
                        per_bundle[i].setdefault(n, {}).update(opts)

        cp = (variant.get("clone_package") or "").strip()
        if cp:
            owner = None
            cname = None
            for i, bundle in enumerate(gen):
                for n in (bundle.get("patches") or {}):
                    if n.lower() == "clone app":
                        owner = i
                        cname = n
            if owner is not None:
                per_bundle[owner][cname] = {"packageName": cp}
            else:
                print(f"{vid}: WARNING clone app patch not found in any bundle")

        an = (variant.get("app_name") or "").strip()
        if an:
            for i, bundle in enumerate(gen):
                for n in (bundle.get("patches") or {}):
                    if n.lower() in ("custom branding", "change app name"):
                        per_bundle[i].setdefault(n, {})["appName"] = an

        wanted = {n: {} for d in per_bundle for n in d}
        skipped = sorted(set().union(*[set(x) for x in needs_per])) if needs_per else []
        print(f"{vid}: enabling {len(wanted)} patches, dup skipped {len(dup_skipped)}, needs skipped {len(skipped)}")

        out_apk = f"build/out_{vid}.apk"
        ok, applied, dropped = heal_patch(apk_path, out_apk, wanted, list(wanted), gen, alias,
                                          f"{vid}_full", bundles=mpps, per_bundle=per_bundle)
        if not applied:
            ok = False

        parts = [f"{b['label']}_v{(b.get('_ver') or '?').lstrip('v')}-{b.get('_status', '?')}" for b in variant["bundles"]]
        joined = "_X_".join(parts)
        if ok and os.path.exists(out_apk):
            fp, match = verify_signature(out_apk, ks_fp)
            final = f"build/{app.get('output_base', aid)}-{appver}-{joined}-patched.apk"
            shutil.copyfile(out_apk, final)
            note = f"## {vid}\n"
            note += f"App version: {appver}\n"
            note += f"Bundles: {', '.join(parts)}\n"
            note += f"Build mode: {mode}\n"
            if fp:
                note += f"Signing fingerprint: {fp}\n"
                if match is False:
                    note += "WARNING: signature differs from keystore\n"
            note += "\nApplied patches:\n"
            note += "\n".join(f"- {a}" for a in applied) if applied else "- none"
            note += "\n"
            if dropped:
                note += "\nDropped after failure (rebuilt without it):\n"
                note += "\n".join(f"- {d}" for d in dropped) + "\n"
            if dup_skipped:
                note += "\nDuplicate of earlier bundle (skipped):\n"
                note += "\n".join(f"- {d}" for d in dup_skipped) + "\n"
            if skipped:
                note += "\nSkipped (needs custom values):\n"
                note += "\n".join(f"- {s}" for s in skipped) + "\n"
            note += "\nStatus: Success\n\n"
            release_notes.append(note)
        else:
            release_notes.append(f"## {vid}\nStatus: Failed\n\n")

def main():
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    exclude = config.get("exclude_patches", []) or []
    auto_on = config.get("auto_include_new_patches", True)

    if os.path.exists('build'): shutil.rmtree('build')
    if os.path.exists('bundles'): shutil.rmtree('bundles')
    os.makedirs('build'); os.makedirs('bundles')

    if os.path.exists("assets/isoamoledbraveicon.png"):
        print("Icon found: assets/isoamoledbraveicon.png")
    else:
        print("WARNING: assets/isoamoledbraveicon.png NOT FOUND in repo!")

    alias = detect_alias()
    print(f"Using signing alias: {alias}")
    ks_fp = keystore_fingerprint(alias)
    print(f"Keystore signing fingerprint: {ks_fp}")

    get_latest_cli_jar()

    dh6k_tag = "unknown"
    rels = requests.get("https://api.github.com/repos/dh6k/morphe-patches/releases").json()
    for r in rels:
        if r.get("draft"):
            continue
        for a in r.get("assets", []):
            if a["name"].endswith(".mpp"):
                download_file(a["browser_download_url"], "bundles/dh6k.mpp")
                dh6k_tag = r.get("tag_name", "unknown")
                break
        else:
            continue
        break

    official_tag = ""
    rel_off = requests.get("https://api.github.com/repos/MorpheApp/morphe-patches/releases/latest").json()
    for a in rel_off.get("assets", []):
        if a["name"].endswith(".mpp"):
            download_file(a["browser_download_url"], "bundles/official.mpp")
            official_tag = rel_off.get("tag_name", "unknown")
            break
    if not official_tag:
        rels2 = requests.get("https://api.github.com/repos/MorpheApp/morphe-patches/releases").json()
        for r in rels2:
            if r.get("draft"):
                continue
            for a in r.get("assets", []):
                if a["name"].endswith(".mpp"):
                    download_file(a["browser_download_url"], "bundles/official.mpp")
                    official_tag = r.get("tag_name", "unknown")
                    break
            else:
                continue
            break
    print(f"Official bundle: {official_tag}")

    extra_bundles = {}
    for eb in config.get("extra_bundles", []) or []:
        mpp = f"bundles/{eb['id']}.mpp"
        try:
            ver = download_bundle_from_json(eb['url'], mpp)
            extra_bundles[eb['id']] = mpp
            print(f"Extra bundle {eb['id']} downloaded: {ver}")
        except Exception as e:
            print(f"Extra bundle {eb['id']} failed: {e}")

    if os.path.exists("bundles/official.mpp") and not is_zip("bundles/official.mpp"):
        print("WARNING: official bundle file invalid, discarding")
        os.remove("bundles/official.mpp")
        official_tag = ""

    BUNDLES.clear()
    BUNDLES.append("bundles/dh6k.mpp")
    if os.path.exists("bundles/official.mpp"):
        BUNDLES.append("bundles/official.mpp")
        print(f"Official bundle loaded: {official_tag}")

    gen_data = generate_options_file(BUNDLES)
    if gen_data is None:
        print("WARNING: could not generate options file")

    info_dh6k = parse_patches_info(["bundles/dh6k.mpp"])

    bundle_names = set()
    for bundle in gen_data or []:
        bundle_names |= set((bundle.get("patches") or {}).keys())
    print(f"Patch names available: {sorted(bundle_names)}")

    needs_value = needs_value_names(gen_data)
    print(f"Patches that need values (never auto-included): {sorted(needs_value)}")

    base_wanted = {
        "Brave Origin": {},
        "Change app icon": {"customIcon": "assets/isoamoledbraveicon.png"},
        "Disable analytics": {}
    }

    def resolve(target, names_set):
        for n in names_set:
            if n.lower() == target:
                return n
        for n in names_set:
            if target in n.lower():
                return n
        return None

    name_patch = resolve("change app name", bundle_names)
    clone_patch = resolve("clone app", bundle_names)
    print(f"Resolved name patch: {name_patch}, clone patch: {clone_patch}")
    configurable = set(base_wanted) | {p for p in (name_patch, clone_patch) if p}

    brave_releases = get_releases("brave/brave-browser")
    latest_stable = get_latest_stable("brave/brave-browser")
    print(f"True stable (Latest badge): {latest_stable.get('tag_name')}")

    apk_cache = {}
    notes = []

    for channel in ["stable", "nightly", "beta"]:
        cands = pick_candidates(brave_releases, channel, latest_stable)

        pin = (PINNED.get(channel) or "").strip()
        if pin:
            pinned = []
            for r in brave_releases:
                if r.get("tag_name") == pin:
                    url = find_asset(r)
                    if url:
                        pinned = [(pin, url)]
                    break
            if pinned:
                cands = pinned
                print(f"{channel}: pinned to {pin}")

        if not cands:
            print(f"No releases found for {channel}")
            continue

        auto_all = compute_auto(info_dh6k, CHANNEL_PKG[channel], exclude, configurable, needs_value) if auto_on else []
        print(f"{channel}: auto-included new patches: {auto_all}")

        probe_tag, probe_applied, probe_dropped, _ = find_version(
            cands, base_wanted, auto_all, gen_data, alias, apk_cache, f"probe_{channel}")

        surviving_auto = [n for n in auto_all if n not in probe_dropped]

        channel_variants = [v for v in config['variants'] if v['type'] == channel]
        for variant in channel_variants:
            wanted = copy.deepcopy(base_wanted)
            skipped = []

            if variant.get("bundles"):
                mpps = []
                for b in variant["bundles"]:
                    if b in extra_bundles:
                        mpps.append(extra_bundles[b])
                    elif os.path.exists(f"bundles/{b}.mpp"):
                        mpps.append(f"bundles/{b}.mpp")
                    else:
                        mpps.append(b)
                gen = generate_options_file(mpps, out_path=f"build/gen_{variant['id']}.json")
                info = parse_patches_info(mpps)
                bundle_names_v = set()
                for bundle in gen or []:
                    bundle_names_v |= set((bundle.get("patches") or {}).keys())
                needs_value_v = needs_value_names(gen)
                name_patch_v = resolve("change app name", bundle_names_v)
                clone_patch_v = resolve("clone app", bundle_names_v)
                configurable_v = set(base_wanted) | {p for p in (name_patch_v, clone_patch_v) if p}
                auto_all_v = compute_auto(info, CHANNEL_PKG[channel], exclude, configurable_v, needs_value_v) if auto_on else []
                surviving_auto_v = [n for n in auto_all_v if n not in probe_dropped]
            else:
                mpps = BUNDLES
                gen = gen_data
                name_patch_v = name_patch
                clone_patch_v = clone_patch
                auto_all_v = auto_all
                surviving_auto_v = surviving_auto

            if variant.get('app_name'):
                if name_patch_v:
                    wanted[name_patch_v] = {"appName": variant['app_name']}
                else:
                    skipped.append("Change app name")
            if variant.get('clone_package'):
                if clone_patch_v:
                    wanted[clone_patch_v] = {"packageName": variant['clone_package']}
                else:
                    skipped.append("Clone app")

            tag, applied, dropped, best_effort = find_version(
                cands, wanted, surviving_auto_v, gen, alias, apk_cache,
                variant['id'], start_tag=probe_tag, bundles=mpps)

            if variant.get("fallback_monochrome") and best_effort:
                print(f"{variant['id']}: universal failed, falling back to kveld9 monochrome APK")
                mono_apk = f"build/base_mono_{variant['id']}.apk"
                mono_tag = download_monochrome_from_kveld9(mono_apk)
                if mono_tag:
                    ok, applied_mono, dropped_mono = heal_patch(mono_apk, f"build/out_{variant['id']}_{mono_tag}.apk",
                                                              wanted, surviving_auto_v, gen, alias, f"{variant['id']}_{mono_tag}", bundles=mpps)
                    if ok:
                        best_effort = False
                        tag = mono_tag
                        applied = applied_mono
                        dropped = dropped_mono

            final_name = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
            src = f"build/out_{variant['id']}_{tag.replace('.', '_')}.apk"
            fp, match = (None, None)
            if os.path.exists(src):
                fp, match = verify_signature(src, ks_fp)
                shutil.copyfile(src, final_name)

            note = f"## {variant['output_name']}\n"
            note += f"Brave version: {tag}\n"
            if variant.get('clone_package') == CHANNEL_PKG['stable']:
                note += "Note: keeps package com.brave.browser; uninstall stock Brave first (signatures differ).\n"
            bundles_note = dh6k_tag + (f", official {official_tag}" if official_tag else "")
            note += f"Patch bundles: {bundles_note}\n"
            if fp:
                note += f"Signing fingerprint: {fp}\n"
                if match is False:
                    note += "WARNING: signature differs from keystore\n"
            note += "\nApplied patches:\n"
            note += "\n".join(f"- {a}" for a in applied) if applied else "- none"
            note += "\n"
            if dropped:
                note += "\nDropped after failure (rebuilt without it):\n"
                note += "\n".join(f"- {d}" for d in dropped) + "\n"
            if skipped:
                note += "\nSkipped (needs custom values or not in bundle):\n"
                note += "\n".join(f"- {s}" for s in skipped) + "\n"
            note += "\nStatus: " + ("Best effort (some patches failed)" if best_effort else "Success") + "\n\n"
            notes.append(note)

    for app in config.get("extra_apps", []) or []:
        build_extra_app(app, alias, ks_fp, notes)

    with open('release_notes.md', 'w') as f:
        f.write("# Morphe AutoBuilds Release\n\n" + "".join(notes))

if __name__ == "__main__":
    main()