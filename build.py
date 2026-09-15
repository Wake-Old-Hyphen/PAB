import os
import re
import json
import yaml
import copy
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

RAW_CACHE = {}
BASE_CACHE = {}


def parse_ver(tag):
    try:
        nums = re.findall(r"\d+", str(tag))[:4]
        return tuple(int(x) for x in nums)
    except Exception:
        return (0,)


def norm_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))


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


def download_file(url, dest, timeout=1200):
    print(f"Downloading {url} ...")
    with requests.get(url, stream=True, timeout=timeout, headers=UA) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)


def download_browser(url, dest, timeout=1200):
    print(f"Downloading browser-style {url} ...")
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=UA, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        if os.path.exists(dest) and os.path.getsize(dest) > 1024 and is_zip(dest):
            return True
    except Exception as e:
        print(f"requests download failed: {e}")

    if HAS_CURL_CFFI:
        for imp in ["chrome136", "chrome133", "chrome131", "chrome124", "chrome120", "chrome110"]:
            try:
                s = cffi_requests.Session(impersonate=imp)
                r = s.get(url, timeout=timeout, headers=UA, allow_redirects=True)
                if r.status_code == 200 and r.content:
                    with open(dest, "wb") as f:
                        f.write(r.content)
                    if os.path.exists(dest) and os.path.getsize(dest) > 1024 and is_zip(dest):
                        return True
            except Exception as e:
                print(f"curl_cffi download failed with {imp}: {e}")
    return False


def cf_get(url, timeout=30):
    print(f"GET {url}")
    if HAS_CURL_CFFI:
        for imp in ["chrome136", "chrome133", "chrome131", "chrome124", "chrome120", "chrome110"]:
            try:
                s = cffi_requests.Session(impersonate=imp)
                r = s.get(url, timeout=timeout, headers=UA, allow_redirects=True)
                text = r.text or ""
                low = text[:1000].lower()
                if r.status_code == 200 and not any(x in low for x in [
                    "just a moment", "attention required", "turnstile", "verify you are human"
                ]):
                    return text
            except Exception:
                continue
    try:
        r = requests.get(url, timeout=timeout, headers=UA, allow_redirects=True)
        if r.status_code == 200:
            text = r.text or ""
            low = text[:1000].lower()
            if not any(x in low for x in ["just a moment", "attention required", "turnstile"]):
                return text
    except Exception as e:
        print(f"plain GET failed: {e}")
    return None


def ensure_apkeep():
    path = "build/apkeep"
    if os.path.exists(path):
        os.chmod(path, 0o755)
        return path

    rel = requests.get("https://api.github.com/repos/EFForg/apkeep/releases/latest", timeout=60).json()
    assets = rel.get("assets", [])
    print("apkeep release assets:", [a.get("name") for a in assets])

    chosen = None
    for a in assets:
        n = a.get("name", "").lower()
        if "linux" in n and "x86_64" in n and not n.endswith((".deb", ".rpm")):
            chosen = a
            break
    if chosen is None:
        for a in assets:
            n = a.get("name", "").lower()
            if "linux" in n and not n.endswith((".deb", ".rpm")):
                chosen = a
                break
    if chosen is None:
        raise Exception("Could not find Linux apkeep release asset")

    raw = "build/apkeep_download"
    download_file(chosen["browser_download_url"], raw)

    extracted = False
    try:
        if tarfile.is_tarfile(raw):
            with tarfile.open(raw, "r:*") as t:
                members = [m for m in t.getmembers() if m.isfile()]
                target = None
                for m in members:
                    if os.path.basename(m.name) == "apkeep":
                        target = m
                        break
                if target is None and members:
                    target = members[0]
                f = t.extractfile(target)
                with open(path, "wb") as out:
                    out.write(f.read())
                extracted = True
        elif zipfile.is_zipfile(raw):
            with zipfile.ZipFile(raw) as z:
                names = [n for n in z.namelist() if os.path.basename(n) == "apkeep"]
                if not names:
                    names = [n for n in z.namelist() if not n.endswith("/")]
                with open(path, "wb") as out:
                    out.write(z.read(names[0]))
                extracted = True
    except Exception as e:
        print(f"apkeep archive extraction failed: {e}")

    if not extracted:
        shutil.copyfile(raw, path)

    os.chmod(path, 0o755)
    return path


def apkeep_download(apkeep, pkg, version, arch, outdir):
    os.makedirs(outdir, exist_ok=True)
    spec = f"{pkg}@{version}" if version and version != "latest" else pkg
    attempts = [
        [apkeep, "-a", spec, "-d", "apk-pure", "-o", f"arch={arch}", outdir],
        [apkeep, "-a", spec, "-o", f"arch={arch}", outdir],
        [apkeep, "-a", spec, outdir],
    ]
    for cmd in attempts:
        try:
            print("Running:", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            print(r.stdout)
            if r.stderr:
                print(r.stderr)
            files = [
                os.path.join(outdir, f)
                for f in os.listdir(outdir)
                if os.path.isfile(os.path.join(outdir, f)) and is_zip(os.path.join(outdir, f))
            ]
            if files:
                files.sort(key=os.path.getsize, reverse=True)
                return files[0]
        except Exception as e:
            print(f"apkeep attempt failed: {e}")
    return None


def select_splits(entries, arch, densities, languages, include_df=True):
    arch_q = arch.replace("-", "_")
    dens = [d.lower() for d in densities]
    langs = [x.lower() for x in languages]
    keep = []
    for n in entries:
        b = os.path.basename(n).lower()
        if not include_df and b.startswith("split_df_"):
            continue
        if ".config." not in b:
            keep.append(n)
            continue
        qual = b.split(".config.")[-1].replace(".apk", "").lower()
        if qual in ABI_QUALS:
            if qual == arch_q:
                keep.append(n)
        elif qual in dens:
            keep.append(n)
        elif qual.isalpha() and len(qual) <= 3:
            if qual in langs:
                keep.append(n)
        else:
            keep.append(n)
    return keep


def raw_kind(raw):
    if not raw or not os.path.exists(raw) or not is_zip(raw):
        return None
    with zipfile.ZipFile(raw) as z:
        names = z.namelist()
    if any(n.lower().endswith(".apk") for n in names):
        return "bundle"
    if any(n.endswith("AndroidManifest.xml") for n in names):
        return "single"
    return None


def save_base(raw, out_apkm, out_single, arch, densities, languages, include_df):
    kind = raw_kind(raw)
    if kind == "bundle":
        with zipfile.ZipFile(raw) as z:
            entries = [n for n in z.namelist() if n.lower().endswith(".apk")]
        keep = select_splits(entries, arch, densities, languages, include_df=include_df)
        if not keep:
            return None, None
        with zipfile.ZipFile(out_apkm, "w", zipfile.ZIP_DEFLATED) as zo:
            for n in keep:
                zo.writestr(os.path.basename(n), z.read(n))
        tag = "no-df" if not include_df else "with-df"
        return out_apkm, f"split subset {tag} ({arch}, {'/'.join(densities)})"
    if kind == "single":
        shutil.copyfile(raw, out_single)
        return out_single, f"single apk ({arch})"
    return None, None


def get_releases(repo):
    r = requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=100", timeout=60)
    r.raise_for_status()
    return r.json()


def get_latest_stable(repo):
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=60)
    r.raise_for_status()
    return r.json()


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
    for pat in [
        r"raw\.githubusercontent\.com/([^/]+/[^/]+)/",
        r"github\.com/([^/]+/[^/]+)/",
        r"bundle/([^/]+/[^/]+)/",
        r"gitlab\.com/([^/]+/[^/]+)/",
    ]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def download_bundle_from_json(url, dest):
    j = requests.get(url, timeout=120).json()
    ver = j.get("version", "unknown")
    dl = j.get("download_url")
    if not dl:
        raise Exception(f"bundle json has no download_url: {url}")
    download_file(dl, dest)
    return ver


def scrape_apkpure_net(spec, version):
    if not HAS_BS4:
        return None
    pkg = spec.get("package", "")
    candidates = []
    for x in [spec.get("apkpure_name"), pkg.split(".")[-1], pkg.replace(".", "-")]:
        if x and x not in candidates:
            candidates.append(x)
    for name in candidates:
        urls = []
        if version and version != "latest":
            urls.append(f"https://apkpure.net/{name}/{pkg}/download/{version}")
        urls.append(f"https://apkpure.net/{name}/{pkg}")
        urls.append(f"https://apkpure.net/{name}/{pkg}/versions")
        for url in urls:
            try:
                html = cf_get(url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                a = soup.find("a", id="download_link")
                if a and a.get("href"):
                    href = a["href"]
                    if not href.startswith("http"):
                        href = urljoin("https://apkpure.net", href)
                    print(f"APKPure link: {href}")
                    return href
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    txt = a.get_text(" ", strip=True).lower()
                    if version and version != "latest" and version not in href and version not in txt:
                        continue
                    if "/download/" in href:
                        if not href.startswith("http"):
                            href = urljoin("https://apkpure.net", href)
                        h2 = cf_get(href)
                        if h2:
                            s2 = BeautifulSoup(h2, "html.parser")
                            b = s2.find("a", id="download_link")
                            if b and b.get("href"):
                                out = b["href"]
                                if not out.startswith("http"):
                                    out = urljoin("https://apkpure.net", out)
                                print(f"APKPure link: {out}")
                                return out
            except Exception as e:
                print(f"APKPure scrape failed for {url}: {e}")
    return None


def scrape_uptodown(spec, version, arch):
    if not HAS_BS4:
        return None
    pkg = spec.get("package", "")
    slugs = []
    for x in [spec.get("uptodown_slug"), pkg.split(".")[-1], pkg.replace(".", "-")]:
        if x and x not in slugs:
            slugs.append(x)
    locales = ["en", "de", "fr", "in", "it", "ru", "jp", "kr"]
    for slug in slugs:
        for loc in locales:
            base = f"https://{slug}.{loc}.uptodown.com/android"
            try:
                html = cf_get(base)
                if not html:
                    continue
                soup = BeautifulSoup(html, "html.parser")
                h1 = soup.find("h1", id="detail-app-name")
                if not h1:
                    continue
                code = h1.get("data-code")
                if not code:
                    continue
                for page in range(1, 11):
                    api = f"{base}/apps/{code}/versions/{page}"
                    js = cf_get(api)
                    if not js:
                        break
                    try:
                        entries = (json.loads(js) or {}).get("data") or []
                    except Exception:
                        break
                    if not entries:
                        break
                    for ent in entries:
                        ev = ent.get("version", "")
                        if version and version != "latest" and ev != version:
                            continue
                        parts = ent.get("versionURL") or {}
                        vu = "/".join(str(parts.get(k, "")).strip("/") for k in ["url", "extraURL", "versionID"])
                        if not vu:
                            continue
                        if not vu.startswith("http"):
                            vu = urljoin(base, vu)
                        vhtml = cf_get(vu)
                        if not vhtml:
                            continue
                        vsoup = BeautifulSoup(vhtml, "html.parser")
                        variant_id = None
                        vbtn = vsoup.select_one(".button.variants[data-version]")
                        if vbtn:
                            data_version = vbtn.get("data-version")
                            cat = f"{base.rsplit('/android', 1)[0]}/app/{code}/version/{data_version}/files"
                            cj = cf_get(cat)
                            if cj:
                                try:
                                    content = (json.loads(cj) or {}).get("content") or ""
                                except Exception:
                                    content = ""
                                csoup = BeautifulSoup(content, "html.parser")
                                cur_arch = ""
                                fallback = None
                                for node in csoup.select("section.variants > .content > *"):
                                    if node.name == "p":
                                        cur_arch = node.get_text(" ", strip=True).lower()
                                        continue
                                    rep = node.select_one(".v-report[data-file-id]")
                                    if not rep:
                                        continue
                                    fid = rep.get("data-file-id")
                                    if not fallback:
                                        fallback = fid
                                    if arch in cur_arch or arch.replace("-", "_") in cur_arch:
                                        variant_id = fid
                                        break
                                if not variant_id:
                                    variant_id = fallback
                        if variant_id:
                            dx = cf_get(f"{base}/download/{variant_id}-x")
                            if dx:
                                dsoup = BeautifulSoup(dx, "html.parser")
                                btn = dsoup.find(id="detail-download-button")
                                if btn and btn.get("data-url"):
                                    out = urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                                    print(f"Uptodown link: {out}")
                                    return out
                        btn = vsoup.find(id="detail-download-button")
                        if btn and btn.get("data-url"):
                            out = urljoin("https://dw.uptodown.com/dwn/", btn["data-url"])
                            print(f"Uptodown link: {out}")
                            return out
                    if version and version != "latest":
                        try:
                            target = parse_ver(version)
                            if all(parse_ver(e.get("version", "")) < target for e in entries):
                                break
                        except Exception:
                            pass
            except Exception as e:
                print(f"Uptodown scrape failed for {base}: {e}")
    return None


def apkmirror_candidate_pages(spec, version):
    base = "https://www.apkmirror.com"
    org = spec.get("apkmirror_org", "google-inc")
    names = spec.get("apkmirror_names") or [spec.get("apkmirror_name", "")]
    names = [n for n in names if n]
    found, seen = [], set()
    ver_slug = version.replace(".", "-") if version else ""

    def add(href):
        if not href:
            return
        if href.startswith("/"):
            href = base + href
        if href not in seen:
            seen.add(href)
            found.append(href)

    for name in names:
        html = cf_get(f"{base}/apk/{org}/{name}/")
        if html:
            for href in re.findall(r'href="([^"]+/apk/[^"]+)"', html):
                if ver_slug and ver_slug not in href:
                    continue
                add(href)
            for href in re.findall(r'href="(/apk/[^"]+)"', html):
                if ver_slug and ver_slug not in href:
                    continue
                add(href)
    if version:
        q = quote_plus(" ".join(names + [version]))
        for su in [f"{base}/?post_type=app_release&searchtype=apk&s={q}", f"{base}/?s={q}"]:
            html = cf_get(su)
            if html:
                for href in re.findall(r'href="(/apk/[^"]+)"', html):
                    if ver_slug in href:
                        add(href)
    return found[:20]


def scrape_apkmirror(spec, version, arch, density):
    base = "https://www.apkmirror.com"
    btype = (spec.get("apkmirror_type") or "APK").upper()
    types = [btype]
    for t in ["BUNDLE", "APK", "ANY"]:
        if t not in types:
            types.append(t)

    pages = apkmirror_candidate_pages(spec, version)
    print(f"APKMirror candidate pages for {version}: {len(pages)}")

    for page_url in pages:
        try:
            page = cf_get(page_url)
            if not page:
                continue
            for t in types:
                rows = re.split(r'<div class="[^"]*table-row[^"]*"[^>]*>', page)
                for row in rows:
                    row_text = re.sub(r"<[^>]+>", " ", row).lower()
                    if "variant" in row_text and "architecture" in row_text:
                        continue
                    badge = re.search(r'apkm-badge[^"]*"[^>]*>\s*([^<]+)\s*</span>', row, re.I)
                    row_type = badge.group(1).strip().upper() if badge else "APK"
                    if t != "ANY" and row_type != t:
                        continue
                    if arch not in row_text and arch.replace("-", "_") not in row_text and "arm64" not in row_text and "universal" not in row_text and "noarch" not in row_text:
                        continue
                    hrefs = [h for h in re.findall(r'href="([^"]+)"', row) if "/apk/" in h]
                    if not hrefs:
                        continue
                    vurl = hrefs[0]
                    if not vurl.startswith("http"):
                        vurl = base + vurl
                    vpage = cf_get(vurl)
                    if not vpage:
                        continue
                    m = (
                        re.search(r'class="[^"]*downloadButton[^"]*"[^>]*href="([^"]+)"', vpage, re.I)
                        or re.search(r'href="([^"]+)"[^>]*class="[^"]*downloadButton', vpage, re.I)
                    )
                    if not m:
                        continue
                    durl = m.group(1)
                    if durl.startswith("/"):
                        durl = base + durl
                    dpage = cf_get(durl)
                    if not dpage:
                        continue
                    m2 = (
                        re.search(r'id="download-link"[^>]*href="([^"]+)"', dpage, re.I)
                        or re.search(r'href="([^"]+)"[^>]*id="download-link"', dpage, re.I)
                    )
                    if not m2:
                        continue
                    out = m2.group(1)
                    if out.startswith("/"):
                        out = base + out
                    print(f"APKMirror link ({t}): {out}")
                    return out
        except Exception as e:
            print(f"APKMirror page failed {page_url}: {e}")
    return None


def find_uploaded_asset(own_repo, up_tag, aid, pkg):
    releases = []
    if up_tag:
        r = requests.get(f"https://api.github.com/repos/{own_repo}/releases/tags/{up_tag}", timeout=60)
        if r.status_code == 200:
            releases.append(r.json())
    r = requests.get(f"https://api.github.com/repos/{own_repo}/releases/latest", timeout=60)
    if r.status_code == 200:
        releases.append(r.json())
    r = requests.get(f"https://api.github.com/repos/{own_repo}/releases?per_page=100", timeout=60)
    if r.status_code == 200:
        releases.extend(r.json())
    seen, uniq = set(), []
    for rel in releases:
        if isinstance(rel, dict) and rel.get("id") not in seen:
            seen.add(rel.get("id"))
            uniq.append(rel)
    ids = [x for x in [aid.lower(), (pkg or "").lower()] if x]

    def ok(name):
        n = name.lower()
        if "patched" in n:
            return False
        if not n.endswith((".apk", ".apkm", ".xapk", ".zip")):
            return False
        return any(x in n for x in ids)

    for rel in uniq:
        for a in rel.get("assets", []):
            if ok(a.get("name", "")):
                return a, rel.get("tag_name")
    return None, None


def download_github_release_apk(spec, dest):
    repo = spec["repo"]
    tag = spec["tag"]
    match = (spec.get("match") or "").lower()
    rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", timeout=60).json()
    assets = rel.get("assets", [])
    chosen = None
    if match:
        for a in assets:
            if match in a.get("name", "").lower():
                chosen = a
                break
    if chosen is None and assets:
        chosen = assets[0]
    if chosen is None:
        raise Exception("No asset found")
    download_file(chosen["browser_download_url"], dest)
    return chosen["name"]


def fetch_raw(app, aid, version, source, bundle_only=False):
    key = (aid, version, bundle_only)
    if key in RAW_CACHE:
        return RAW_CACHE[key]

    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    default_version = str(spec.get("version") or "latest")
    result = (None, None)

    def accept(raw):
        k = raw_kind(raw)
        if k is None:
            return None
        if bundle_only and k != "bundle":
            return None
        return k

    if not bundle_only and source in ("upload", "apkeep", "scraper"):
        up_tag = (spec.get("upload_tag") or "").strip()
        own_repo = os.environ.get("GITHUB_REPOSITORY", "")
        if up_tag and own_repo and version == default_version:
            asset, tag = find_uploaded_asset(own_repo, up_tag, aid, spec.get("package", ""))
            if asset:
                print(f"{aid}: using uploaded asset {asset['name']} from release {tag}")
                raw = f"build/raw_{aid}_{safe_name(version)}_upload.bin"
                try:
                    download_file(asset["browser_download_url"], raw)
                    k = accept(raw)
                    if k:
                        result = (raw, k)
                except Exception as e:
                    print(f"{aid}: uploaded asset failed: {e}")

    if result[0] is None and source in ("apkeep", "scraper", "upload"):
        raw = f"build/raw_{aid}_{safe_name(version)}_scraper.bin"
        for label, fn in [
            ("apkmirror", lambda: scrape_apkmirror(spec, version, arch, app.get("density", "xxhdpi"))),
            ("apkpure.net", lambda: scrape_apkpure_net(spec, version)),
            ("uptodown", lambda: scrape_uptodown(spec, version, arch)),
        ]:
            try:
                dl = fn()
                if not dl:
                    print(f"{aid}: {label} did not find a link for {version}")
                    continue
                if download_browser(dl, raw):
                    k = accept(raw)
                    if k:
                        print(f"{aid}: {label} provided {k} for {version}")
                        result = (raw, k)
                        break
                    else:
                        print(f"{aid}: {label} result not acceptable (bundle_only={bundle_only})")
                else:
                    print(f"{aid}: {label} download invalid")
            except Exception as e:
                print(f"{aid}: {label} failed: {e}")

    if result[0] is None and source == "apkeep":
        try:
            apkeep = ensure_apkeep()
            raw = apkeep_download(apkeep, spec.get("package", ""), version, arch, f"build/apkeep_{aid}_{safe_name(version)}")
            if raw:
                k = accept(raw)
                if k:
                    result = (raw, k)
        except Exception as e:
            print(f"{aid}: apkeep failed: {e}")

    if result[0] is None and source in ("apkeep", "scraper") and version == default_version:
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
                except Exception as e:
                    print(f"{aid}: direct APKPure failed: {e}")

    if result[0] is None and version == default_version and spec.get("repo") and spec.get("tag") and not bundle_only:
        try:
            raw = f"build/base_{aid}_gh.apk"
            download_github_release_apk(spec, raw)
            k = accept(raw)
            if k:
                result = (raw, k)
        except Exception as e:
            print(f"{aid}: github fallback failed: {e}")

    RAW_CACHE[key] = result
    return result


def prepare_bases(app, aid, version, source, arch, densities, languages):
    key = (aid, version)
    if key in BASE_CACHE:
        return BASE_CACHE[key]

    sv = safe_name(version)
    out_full_apkm = f"build/base_{aid}_{sv}_full.apkm"
    out_slim_apkm = f"build/base_{aid}_{sv}_slim.apkm"
    out_single = f"build/base_{aid}_{sv}.apk"

    raw, kind = fetch_raw(app, aid, version, source, bundle_only=False)
    if raw is None:
        BASE_CACHE[key] = None
        return None

    if kind == "bundle":
        full = save_base(raw, out_full_apkm, out_single, arch, densities, languages, True)
        slim = save_base(raw, out_slim_apkm, out_single, arch, densities, languages, False)
        res = {"full": full, "slim": slim, "single": False}
    else:
        full = save_base(raw, out_full_apkm, out_single, arch, densities, languages, True)
        raw2, kind2 = fetch_raw(app, aid, version, source, bundle_only=True)
        if kind2 == "bundle":
            slim = save_base(raw2, out_slim_apkm, out_single, arch, densities, languages, False)
            res = {"full": full, "slim": slim, "single": False}
        else:
            res = {"full": full, "slim": full, "single": True}

    BASE_CACHE[key] = res
    return res


def generate_options_file(bundles, out_path):
    for sub in ["options", "options-create"]:
        cmd = ["java", "-jar", "build/cli.jar", sub]
        for b in bundles:
            cmd += ["-p", b]
        cmd += ["-o", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(out_path):
            with open(out_path) as f:
                return json.load(f)
    return None


def set_option_value(opts, key, value):
    cur = opts.get(key)
    if isinstance(cur, dict):
        cur["value"] = value
    else:
        opts[key] = value


def apply_option(entry, key, value):
    opts = entry.setdefault("options", {})
    nk = norm_key(key)
    for k in list(opts):
        if k == key or norm_key(k) == nk:
            set_option_value(opts, k, value)
            return
    for k in list(opts):
        ok = norm_key(k)
        if nk and ok and (nk in ok or ok in nk):
            set_option_value(opts, k, value)
            return
    if len(opts) == 1:
        set_option_value(opts, list(opts)[0], value)
        return
    set_option_value(opts, key, value)


def enable_entry(entry, vals):
    entry["enabled"] = True
    vals = vals or {}
    for k, v in vals.items():
        apply_option(entry, k, v)
    pkg = vals.get("packageName") or vals.get("packagename")
    if pkg:
        for k in list((entry.get("options") or {})):
            kl = k.lower()
            raw = entry["options"][k]
            cur = raw.get("value") if isinstance(raw, dict) else raw
            if "update" in kl and isinstance(cur, bool):
                set_option_value(entry["options"], k, True)
            elif "package" in kl and "update" not in kl:
                set_option_value(entry["options"], k, pkg)


def make_variant_options(gen_data, per_bundle):
    data = copy.deepcopy(gen_data)
    found = set()
    all_wanted = set()
    for i, bundle in enumerate(data):
        wanted = per_bundle[i] if i < len(per_bundle) else {}
        all_wanted |= set(wanted)
        for name, entry in (bundle.get("patches") or {}).items():
            if name in wanted:
                found.add(name)
                enable_entry(entry, wanted[name])
            else:
                entry["enabled"] = False
    missing = sorted(all_wanted - found)
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
            if "PrivateKeyEntry" in line or "trustedCertEntry" in line:
                alias = line.split(",")[0].strip()
                if alias:
                    aliases.append(alias)
        print("Aliases found:", aliases)
        if preferred in aliases:
            return preferred
        if aliases:
            return aliases[0]
    except Exception as e:
        print("Alias detection failed:", e)
    return preferred


def keystore_fingerprint(alias):
    ks = "signing/keystore.jks"
    pw = os.environ.get("KEYSTORE_PASSWORD", "")
    try:
        r = subprocess.run(["keytool", "-list", "-v", "-keystore", ks, "-storepass", pw, "-alias", alias],
                           capture_output=True, text=True)
        m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
        return m.group(1).upper() if m else None
    except Exception:
        return None


def apk_fingerprint(path):
    try:
        r = subprocess.run(["keytool", "-printcert", "-jarfile", path], capture_output=True, text=True)
        m = re.search(r"SHA-?256:\s*([0-9A-Fa-f:]+)", r.stdout)
        return m.group(1).upper() if m else None
    except Exception:
        return None


def verify_signature(path, ks_fp):
    fp = apk_fingerprint(path)
    if fp:
        print(f"signature check: apk={fp} keystore={ks_fp} match={fp == ks_fp if ks_fp else None}")
    return fp


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
            cur = {"name": None, "packages": [], "required_opts": [], "last_key": None}
            info.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Package name:"):
            cur["packages"].append(line.split(":", 1)[1].strip())
        elif line.startswith("Key:"):
            cur["last_key"] = line.split(":", 1)[1].strip()
            if pending_required:
                cur["required_opts"].append(cur["last_key"])
                pending_required = False
        elif line.startswith("Required:"):
            req = line.split(":", 1)[1].strip().lower() == "true"
            if req and cur.get("last_key"):
                cur["required_opts"].append(cur["last_key"])
            else:
                pending_required = req
    return [x for x in info if x.get("name")]


def compute_auto(info, pkg, exclude, configured, needs_value):
    ex = {x.lower() for x in exclude}
    conf = {x.lower() for x in configured}
    needs = {x.lower() for x in needs_value}
    out = []
    for p in info:
        n = p["name"]
        nl = n.lower()
        if nl in ex or nl in conf or nl in needs:
            continue
        if p.get("required_opts"):
            continue
        if pkg in p.get("packages", []):
            out.append(n)
    return out


def run_patch(apk_path, out_apk, gen_data, per_bundle, label, alias, bundles):
    data, missing = make_variant_options(gen_data, per_bundle)
    opts_path = f"build/options_{safe_name(label)}.json"
    with open(opts_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"OPTIONS FILE FOR {label}:")
    print(open(opts_path).read())

    cmd = ["java", "-jar", "build/cli.jar", "patch"]
    for b in bundles:
        cmd += ["-p", b]
    cmd += ["--options-file", opts_path]
    ks = "signing/keystore.jks"
    if os.path.exists(ks):
        cmd += [
            "--keystore", ks,
            "--keystore-password", os.environ.get("KEYSTORE_PASSWORD", ""),
            "--keystore-entry-alias", alias,
            "--keystore-entry-password", os.environ.get("KEY_PASSWORD", ""),
        ]
    cmd += ["--striplibs", "arm64-v8a", "-o", out_apk, "--continue-on-error", apk_path]
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


def heal_patch(apk_path, out_apk, gen_data, per_bundle, label, alias, bundles):
    pb = [dict(x) for x in per_bundle]
    dropped = []
    while True:
        ok, applied, failed, missing = run_patch(apk_path, out_apk, gen_data, pb, label, alias, bundles)
        if ok:
            return True, applied, dropped, missing
        if not failed:
            return False, applied, dropped, missing
        removed = False
        for f in failed:
            fl = f.lower()
            for d in pb:
                for n in list(d):
                    if n.lower() == fl:
                        print(f"Dropping failed patch and retrying: {n}")
                        dropped.append(n)
                        del d[n]
                        removed = True
        if not removed:
            return False, applied, dropped, missing


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
        if ok:
            return tag, out, applied, dropped, False
    tag, url = cands[0]
    apk = get_apk(tag, url, cache)
    out = f"build/out_{safe_name(label)}_{safe_name(tag)}.apk"
    ok, applied, dropped, missing = heal_patch(apk, out, gen_data, per_bundle, f"{label}_besteffort", alias, bundles)
    return tag, out, applied, dropped, True


def get_latest_cli_jar():
    rel = requests.get("https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest", timeout=60).json()
    for a in rel.get("assets", []):
        if a.get("name", "").endswith("-all.jar"):
            download_file(a["browser_download_url"], "build/cli.jar")
            return
    raise Exception("Could not find Morphe CLI all.jar")


BRANDING_KEYWORDS = (
    "custom branding",
    "change app name",
    "change app name and icon",
    "change app icon",
    "custom app icon",
    "custom icon",
)

def is_branding_patch(name):
    nl = (name or "").lower()
    return any(k in nl for k in BRANDING_KEYWORDS)


def build_extra_app(app, alias, ks_fp, notes):
    aid = app["id"]
    print(f"\n=== Extra app: {aid} ===")

    spec = app["apk"]
    arch = spec.get("arch", "arm64-v8a")
    dens = app.get("density", ["xxhdpi"])
    if isinstance(dens, str):
        dens = [dens]
    languages = [x.lower() for x in app.get("languages", ["en"])]
    default_version = str(spec.get("version") or "latest")

    variants = app.get("variants", [])

    for v in variants:
        mpps = []
        ok = True
        for b in v.get("bundles", []):
            mpp = f"bundles/{aid}_{safe_name(b['label'])}.mpp"
            try:
                ver = download_bundle_from_json(b["url"], mpp)
                b["_ver"] = ver
                b["_status"] = get_release_status(repo_from_url(b["url"]), ver)
                mpps.append(mpp)
            except Exception as e:
                print(f"{v['id']}: bundle failed {b['label']}: {e}")
                ok = False
                break
        v["_ok"] = ok
        v["_mpps"] = mpps

    for v in variants:
        vid = v["id"]
        if not v.get("_ok"):
            notes.append(f"## {vid}\nStatus: Failed (bundle download)\n\n")
            continue

        ver = str(v.get("apk_version") or default_version)
        vsource = v.get("apk_source", spec.get("source", "apkeep"))

        bases = prepare_bases(app, aid, ver, vsource, arch, dens, languages)
        if not bases:
            notes.append(f"## {vid}\nStatus: Failed (apk source {ver})\n\n")
            continue

        mpps = v["_mpps"]
        gen = generate_options_file(mpps, f"build/gen_{safe_name(vid)}.json")
        if gen is None:
            notes.append(f"## {vid}\nStatus: Failed (options generation)\n\n")
            continue

        per_bundle = []
        seen_lower = set()
        dup_skipped = []
        for i, g in enumerate(gen):
            bundle_cfg = v.get("bundles", [])[i] if i < len(v.get("bundles", [])) else {}
            allow = bundle_cfg.get("patches")
            allow_l = {x.lower() for x in allow} if allow else None
            wanted = {}
            for name in sorted((g.get("patches") or {}).keys()):
                nl = name.lower()
                if allow_l is not None and nl not in allow_l:
                    continue
                if v.get("merge_exclusive") and nl in seen_lower:
                    dup_skipped.append(name)
                    continue
                wanted[name] = {}
                seen_lower.add(nl)
            per_bundle.append(wanted)

        excludes = {x.lower() for x in v.get("exclude_patches", [])}
        for d in per_bundle:
            for n in list(d):
                if n.lower() in excludes:
                    del d[n]

        for patch_name, opts in (v.get("options") or {}).items():
            pl = patch_name.lower()
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() == pl:
                        per_bundle[i].setdefault(real, {}).update(opts)

        cp = (v.get("clone_package") or "").strip()
        if cp:
            owner = None
            cname = None
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() == "clone app":
                        owner = i
                        cname = real
            if owner is not None:
                per_bundle[owner][cname] = {"packageName": cp}
            else:
                print(f"{vid}: Clone app patch not found")

        an = (v.get("app_name") or "").strip()
        if an:
            for i, g in enumerate(gen):
                for real in (g.get("patches") or {}):
                    if real.lower() in ("custom branding", "change app name"):
                        per_bundle[i].setdefault(real, {})["appName"] = an

        parts = [f"{b['label']}_v{str(b.get('_ver', '?')).lstrip('v')}-{b.get('_status', '?')}" for b in v["bundles"]]
        joined = "_X_".join(parts)

        modes = ["slim"]
        if not bases["single"]:
            modes.append("full")

        for mode in modes:
            bp, bmode = bases[mode]
            out = f"build/out_{safe_name(vid)}_{mode}.apk"
            ok, applied, dropped, missing = heal_patch(bp, out, gen, per_bundle, f"{vid}_{mode}", alias, mpps)

            if mode == "slim":
                final = f"build/{app.get('output_base', aid)}-{ver}-{joined}-patched.apk"
                head = f"## {vid}"
            else:
                final = f"build/{app.get('output_base', aid)}-{ver}-{joined}-fulldf-patched.apk"
                head = f"## {vid} (full dynamic features)"

            if ok and os.path.exists(out):
                shutil.copyfile(out, final)
                fp = verify_signature(final, ks_fp)
                note = f"{head}\n"
                note += f"App version: {ver}\n"
                note += f"Bundles: {', '.join(parts)}\n"
                note += f"Build mode: {bmode}\n"
                if bases["single"]:
                    note += "Note: base is a single APK; no dynamic-feature splits to remove.\n"
                if fp:
                    note += f"Signing fingerprint: {fp}\n"
                note += "\nApplied patches:\n"
                note += "\n".join(f"- {x}" for x in applied) if applied else "- none"
                note += "\n"
                if dropped:
                    note += "\nDropped after failure:\n" + "\n".join(f"- {x}" for x in dropped) + "\n"
                if dup_skipped:
                    note += "\nDuplicate patches skipped:\n" + "\n".join(f"- {x}" for x in dup_skipped) + "\n"
                note += "\nStatus: Success\n\n"
                notes.append(note)
            else:
                notes.append(f"{head}\nStatus: Failed\n\n")


def download_monochrome_from_kveld9(dest):
    try:
        rel = requests.get("https://api.github.com/repos/kveld9/kveld-morphe-patches/releases/latest", timeout=60).json()
        for a in rel.get("assets", []):
            n = a.get("name", "").lower()
            if "mono" in n and n.endswith(".apk"):
                download_file(a["browser_download_url"], dest)
                return rel.get("tag_name", "kveld9-mono")
    except Exception as e:
        print("kveld9 mono fallback failed:", e)
    return None


def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    if os.path.exists("build"):
        shutil.rmtree("build")
    if os.path.exists("bundles"):
        shutil.rmtree("bundles")
    os.makedirs("build")
    os.makedirs("bundles")

    alias = detect_alias()
    ks_fp = keystore_fingerprint(alias)
    print(f"Using signing alias: {alias}")
    print(f"Keystore fingerprint: {ks_fp}")

    get_latest_cli_jar()

    dh6k_tag = "unknown"
    rels = requests.get("https://api.github.com/repos/dh6k/morphe-patches/releases", timeout=60).json()
    for r in rels:
        if r.get("draft"):
            continue
        for a in r.get("assets", []):
            if a.get("name", "").endswith(".mpp"):
                download_file(a["browser_download_url"], "bundles/dh6k.mpp")
                dh6k_tag = r.get("tag_name", "unknown")
                break
        if os.path.exists("bundles/dh6k.mpp"):
            break

    official_tag = "unknown"
    rel = requests.get("https://api.github.com/repos/MorpheApp/morphe-patches/releases/latest", timeout=60).json()
    for a in rel.get("assets", []):
        if a.get("name", "").endswith(".mpp"):
            download_file(a["browser_download_url"], "bundles/official.mpp")
            official_tag = rel.get("tag_name", "unknown")
            break

    extra_bundles = {}
    for eb in config.get("extra_bundles", []):
        try:
            mpp = f"bundles/{eb['id']}.mpp"
            ver = download_bundle_from_json(eb["url"], mpp)
            extra_bundles[eb["id"]] = mpp
            print(f"Extra bundle {eb['id']}: {ver}")
        except Exception as e:
            print(f"Extra bundle {eb['id']} failed: {e}")

    BUNDLES.clear()
    BUNDLES.append("bundles/dh6k.mpp")
    if os.path.exists("bundles/official.mpp"):
        BUNDLES.append("bundles/official.mpp")

    gen_brave = generate_options_file(BUNDLES, "build/gen_brave.json")
    info_dh6k = parse_patches_info(["bundles/dh6k.mpp"])
    needs_brave = needs_value_names(gen_brave)

    all_names = set()
    for g in gen_brave or []:
        all_names |= set((g.get("patches") or {}).keys())

    def resolve(target, names):
        target = target.lower()
        for n in names:
            if n.lower() == target:
                return n
        for n in names:
            if target in n.lower():
                return n
        return None

    brave_base = {
        "Brave Origin": {},
        "Change app icon": {"customIcon": "assets/isoamoledbraveicon.png"},
        "Disable analytics": {},
    }

    name_patch = resolve("change app name", all_names)
    clone_patch = resolve("clone app", all_names)

    brave_releases = get_releases("brave/brave-browser")
    latest_stable = get_latest_stable("brave/brave-browser")

    cache = {}
    notes = []

    for channel in ["stable", "nightly", "beta"]:
        cands = pick_candidates(brave_releases, channel, latest_stable)
        if not cands:
            continue

        configurable = set(brave_base)
        if name_patch:
            configurable.add(name_patch)
        if clone_patch:
            configurable.add(clone_patch)

        auto = compute_auto(
            info_dh6k,
            CHANNEL_PKG[channel],
            config.get("exclude_patches", []),
            configurable,
            needs_brave,
        ) if config.get("auto_include_new_patches", True) else []

        for variant in [v for v in config["variants"] if v["type"] == channel]:
            bundles = BUNDLES
            gen = gen_brave
            names = all_names
            n_patch = name_patch
            c_patch = clone_patch

            if variant.get("bundles"):
                bundles = []
                for bid in variant["bundles"]:
                    if bid in extra_bundles:
                        bundles.append(extra_bundles[bid])
                    elif os.path.exists(f"bundles/{bid}.mpp"):
                        bundles.append(f"bundles/{bid}.mpp")
                gen = generate_options_file(bundles, f"build/gen_{variant['id']}.json")
                names = set()
                for g in gen or []:
                    names |= set((g.get("patches") or {}).keys())
                n_patch = resolve("change app name", names)
                c_patch = resolve("clone app", names)

            per_bundle = []
            for g in gen:
                d = {}
                for n in sorted((g.get("patches") or {}).keys()):
                    if n in brave_base:
                        d[n] = brave_base[n]
                    elif n in auto:
                        d[n] = {}
                per_bundle.append(d)

            if variant.get("app_name") and n_patch:
                for d in per_bundle:
                    if n_patch in d or n_patch in names:
                        d[n_patch] = {"appName": variant["app_name"]}
                        break

            if variant.get("clone_package") and c_patch:
                for d in per_bundle:
                    if c_patch in d or c_patch in names:
                        d[c_patch] = {"packageName": variant["clone_package"]}
                        break

            tag, out, applied, dropped, best_effort = find_working_brave_version(
                cands, per_bundle, gen, alias, cache, variant["id"], bundles
            )

            if variant.get("fallback_monochrome") and best_effort:
                mono = f"build/base_mono_{variant['id']}.apk"
                mono_tag = download_monochrome_from_kveld9(mono)
                if mono_tag:
                    out2 = f"build/out_{variant['id']}_{safe_name(mono_tag)}.apk"
                    ok, applied2, dropped2, missing2 = heal_patch(mono, out2, gen, per_bundle, variant["id"], alias, bundles)
                    if ok:
                        tag = mono_tag
                        out = out2
                        applied = applied2
                        dropped = dropped2
                        best_effort = False

            final = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
            if os.path.exists(out):
                shutil.copyfile(out, final)
                fp = verify_signature(final, ks_fp)
            else:
                fp = None

            note = f"## {variant['output_name']}\n"
            note += f"Brave version: {tag}\n"
            note += f"Patch bundles: {dh6k_tag}, official {official_tag}\n"
            if fp:
                note += f"Signing fingerprint: {fp}\n"
            note += "\nApplied patches:\n"
            note += "\n".join(f"- {x}" for x in applied) if applied else "- none"
            note += "\n"
            if dropped:
                note += "\nDropped after failure:\n" + "\n".join(f"- {x}" for x in dropped) + "\n"
            note += "\nStatus: " + ("Best effort" if best_effort else "Success") + "\n\n"
            notes.append(note)

    for app in config.get("extra_apps", []):
        build_extra_app(app, alias, ks_fp, notes)

    with open("release_notes.md", "w") as f:
        f.write("# Morphe AutoBuilds Release\n\n" + "".join(notes))


if __name__ == "__main__":
    main()