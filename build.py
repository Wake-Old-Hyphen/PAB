import os
import re
import yaml
import json
import copy
import subprocess
import requests
import shutil

MAX_ATTEMPTS = 5

PINNED = {"stable": "", "nightly": "", "beta": ""}

CHANNEL_PKG = {
    "stable": "com.brave.browser",
    "beta": "com.brave.browser_beta",
    "nightly": "com.brave.browser_nightly"
}

BUNDLES = []

def parse_ver(tag):
    try:
        nums = (tag or "").lower().lstrip("v").split(".")
        return tuple(int(x) for x in nums if x.isdigit())[:3]
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

def is_valid_mpp(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except Exception:
        return False

def get_releases(repo):
    return requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=100").json()

def get_latest_stable(repo):
    return requests.get(f"https://api.github.com/repos/{repo}/releases/latest").json()

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

def generate_options_file(bundles):
    for sub in ("options", "options-create"):
        cmd = ["java", "-jar", "build/cli.jar", sub]
        for b in bundles:
            cmd += ["-p", b]
        cmd += ["-o", "build/gen_options.json"]
        r = subprocess.run(cmd)
        if r.returncode == 0 and os.path.exists("build/gen_options.json"):
            with open("build/gen_options.json") as f:
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

def apply_option(entry, key, value):
    opts = entry.setdefault("options", {})
    if key in opts:
        set_option_value(opts, key, value)
        return
    for k in list(opts):
        if k.lower() == key.lower():
            set_option_value(opts, k, value)
            return
    if len(opts) == 1:
        set_option_value(opts, list(opts)[0], value)
        return
    set_option_value(opts, key, value)

def make_variant_options(gen_data, wanted):
    data = copy.deepcopy(gen_data)
    found = set()
    for bundle in data:
        patches = bundle.get("patches", {})
        for name, entry in patches.items():
            if name in wanted:
                found.add(name)
                entry["enabled"] = True
                for k, v in wanted[name].items():
                    apply_option(entry, k, v)
            else:
                entry["enabled"] = False
    missing = [n for n in wanted if n not in found]
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
            cur["packages"].append(line.split(":", 1)[1].strip())
    return [p for p in info if p["name"]]

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

def run_patch(apk_path, out_apk, wanted, gen_data, label, alias):
    cmd = ["java", "-jar", "build/cli.jar", "patch"]
    for b in BUNDLES:
        cmd += ["-p", b]
    missing = []
    if gen_data is not None:
        data, missing = make_variant_options(gen_data, wanted)
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

def heal_patch(apk, out_apk, wanted, auto, gen_data, alias, label):
    auto = list(auto)
    dropped = []
    while True:
        full = dict(wanted)
        for n in auto:
            if n not in full:
                full[n] = {}
        ok, applied, failed, missing = run_patch(apk, out_apk, full, gen_data, label, alias)
        if ok:
            return True, applied, dropped
        auto_failed = [n for n in failed if n in auto]
        if not auto_failed:
            return False, applied, dropped
        for n in auto_failed:
            auto.remove(n)
            dropped.append(n)

def get_apk(tag, url, cache):
    if tag not in cache:
        path = f"build/base_{tag.replace('.', '_')}.apk"
        download_file(url, path)
        cache[tag] = path
    return cache[tag]

def find_version(cands, wanted, auto_all, gen_data, alias, cache, label, start_tag=None):
    ordered = cands
    if start_tag:
        ordered = [c for c in cands if c[0] == start_tag] + [c for c in cands if c[0] != start_tag]
    for tag, url in ordered:
        apk = get_apk(tag, url, cache)
        ok, applied, dropped = heal_patch(apk, f"build/out_{label}_{tag.replace('.', '_')}.apk",
                                          wanted, auto_all, gen_data, alias, f"{label}_{tag}")
        if ok:
            print(f"{label}: working version -> {tag}")
            return tag, applied, dropped, False
        print(f"{label}: {tag} not workable, trying older...")
    tag, url = ordered[0]
    apk = get_apk(tag, url, cache)
    ok, applied, dropped = heal_patch(apk, f"build/out_{label}_{tag.replace('.', '_')}.apk",
                                      wanted, auto_all, gen_data, alias, f"{label}_besteffort")
    print(f"{label}: no fully working version, best effort on {tag}")
    return tag, applied, dropped, True

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
    rels2 = requests.get("https://api.github.com/repos/MorpheApp/morphe-patches/releases").json()
    for r in rels2:
        if r.get("draft") or not r.get("prerelease"):
            continue
        for a in r.get("assets", []):
            if a["name"].endswith(".mpp"):
                download_file(a["browser_download_url"], "bundles/official.mpp")
                official_tag = r.get("tag_name", "unknown")
                break
        else:
            continue
        break

    if os.path.exists("bundles/official.mpp") and not is_valid_mpp("bundles/official.mpp"):
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

    # Authoritative patch names: the options file the CLI itself generated
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

    def resolve(target):
        for n in bundle_names:
            if n.lower() == target:
                return n
        for n in bundle_names:
            if target in n.lower():
                return n
        return None

    name_patch = resolve("change app name")
    clone_patch = resolve("clone app")
    print(f"Resolved name patch: {name_patch}, clone patch: {clone_patch}")
    configurable = set(base_wanted) | {p for p in (name_patch, clone_patch) if p}

    brave_releases = get_releases("brave/brave-browser")
    latest_stable = get_latest_stable("brave/brave-browser")
    print(f"True stable (Latest badge): {latest_stable.get('tag_name')}")

    apk_cache = {}
    release_notes = "# Morphe AutoBuilds Release\n\n"

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

            if variant.get('app_name'):
                if name_patch:
                    wanted[name_patch] = {"appName": variant['app_name']}
                else:
                    skipped.append("Change app name")
            if variant.get('clone_package'):
                if clone_patch:
                    wanted[clone_patch] = {"packageName": variant['clone_package']}
                else:
                    skipped.append("Clone app")

            tag, applied, dropped, best_effort = find_version(
                cands, wanted, surviving_auto, gen_data, alias, apk_cache,
                variant['id'], start_tag=probe_tag)

            final_name = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
            src = f"build/out_{variant['id']}_{tag.replace('.', '_')}.apk"
            if os.path.exists(src):
                shutil.copyfile(src, final_name)

            release_notes += f"## {variant['output_name']}\n"
            release_notes += f"Brave version: {tag}\n"
            bundles_note = dh6k_tag + (f", official {official_tag}" if official_tag else "")
            release_notes += f"Patch bundles: {bundles_note}\n\n"
            release_notes += "Applied patches:\n"
            release_notes += "\n".join(f"- {a}" for a in applied) if applied else "- none"
            release_notes += "\n"
            if dropped:
                release_notes += "\nDropped after failure (rebuilt without it):\n"
                release_notes += "\n".join(f"- {d}" for d in dropped) + "\n"
            if skipped:
                release_notes += "\nSkipped (needs custom values or not in bundle):\n"
                release_notes += "\n".join(f"- {s}" for s in skipped) + "\n"
            release_notes += "\nStatus: " + ("Best effort (some patches failed)" if best_effort else "Success") + "\n\n"

    with open('release_notes.md', 'w') as f:
        f.write(release_notes)

if __name__ == "__main__":
    main()