import os
import yaml
import subprocess
import requests
import shutil

MAX_ATTEMPTS = 5  # max tries per channel (stable is always added as last resort)

# OPTIONAL PINS: put an exact tag here (e.g. "v1.94.117") to force a version.
PINNED = {
    "stable": "",
    "nightly": "",
    "beta": ""
}

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

def get_releases(repo):
    return requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=100").json()

def get_latest_stable(repo):
    # Exactly the release with the green "Latest" badge
    return requests.get(f"https://api.github.com/repos/{repo}/releases/latest").json()

def find_asset(release):
    for a in release.get("assets", []):
        n = a["name"].lower()
        if "arm64" in n and "universal" in n:
            return a["browser_download_url"]
    return None

def classify(r, stable_tag):
    # Classify by TITLE keywords (badges are unreliable for beta)
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
    else:  # nightly
        cands = nightlies + betas + ([stable_cand] if stable_cand else []) + older

    # dedupe, keep order
    seen = set()
    out = []
    for t, u in cands:
        if t not in seen:
            seen.add(t)
            out.append((t, u))

    out = out[:MAX_ATTEMPTS]
    # ALWAYS keep stable as the final safety net
    if stable_cand and stable_cand[0] not in [t for t, _ in out]:
        out.append(stable_cand)
    return out

def list_patch_names(bundle):
    out = subprocess.run(["java", "-jar", "build/cli.jar", "list-patches", "-p", bundle],
                         capture_output=True, text=True)
    names = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            names.append(line[5:].strip())
    return names

def run_patch(apk_path, out_apk, enable, options, continue_on_error=False):
    cmd = ["java", "-jar", "build/cli.jar", "patch", "-p", "bundles/dh6k.mpp", "-o", out_apk, apk_path]
    for p in enable:
        cmd += ["-e", p]
    for k, v in options.items():
        cmd += ["-O", f"{k}={v}"]
    if continue_on_error:
        cmd.append("--continue-on-error")
    print("Running:", " ".join(cmd))
    return subprocess.run(cmd).returncode == 0

def main():
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    if os.path.exists('build'): shutil.rmtree('build')
    if os.path.exists('bundles'): shutil.rmtree('bundles')
    os.makedirs('build'); os.makedirs('bundles')

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

    available = list_patch_names("bundles/dh6k.mpp")
    print("Available patches in bundle:", available)

    base_patches = ["Brave Origin", "Change app icon", "Disable analytics"]
    probe_enable = [p for p in base_patches if p in available]
    probe_opts = {"customIcon": "assets/isoamoledbraveicon.png"}

    brave_releases = get_releases("brave/brave-browser")
    latest_stable = get_latest_stable("brave/brave-browser")
    print(f"True stable (Latest badge): {latest_stable.get('tag_name')}")

    channel_info = {}
    for channel in ["stable", "nightly", "beta"]:
        cands = pick_candidates(brave_releases, channel, latest_stable)
        print(f"{channel} try-order: {[t for t, _ in cands]}")

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

        chosen_tag = cands[0][0]
        best_effort = True
        for tag, url in cands:
            apk = f"build/base_{channel}.apk"
            download_file(url, apk)
            if run_patch(apk, f"build/probe_{channel}.apk", probe_enable, probe_opts):
                chosen_tag = tag
                best_effort = False
                print(f"{channel}: compatible version found -> {tag}")
                break
            else:
                print(f"{channel}: {tag} incompatible, trying older...")
        if best_effort:
            download_file(cands[0][1], f"build/base_{channel}.apk")
            chosen_tag = cands[0][0]
            print(f"{channel}: no compatible version found, using first candidate (best effort)")
        channel_info[channel] = (chosen_tag, f"build/base_{channel}.apk", best_effort)

    release_notes = "# Morphe AutoBuilds Release\n\n"

    for variant in config['variants']:
        channel = variant['type']
        if channel not in channel_info:
            release_notes += f"## {variant['output_name']}\n- Status: No APK found for this channel\n\n"
            continue

        tag, apk, best_effort = channel_info[channel]

        enable = [p for p in base_patches if p in available]
        opts = {"customIcon": "assets/isoamoledbraveicon.png"}
        skipped = []

        if variant.get('app_name'):
            if "Change app name" in available:
                enable.append("Change app name")
                opts["appName"] = variant['app_name']
            else:
                skipped.append("Change app name")

        if variant.get('clone_package'):
            if "Clone app" in available:
                enable.append("Clone app")
                opts["packageName"] = variant['clone_package']
            else:
                skipped.append("Clone app")

        out_apk = f"build/{variant['output_name']}-{tag}-{dh6k_tag}-patched.apk"
        ok = run_patch(apk, out_apk, enable, opts, continue_on_error=best_effort)

        if ok or (best_effort and os.path.exists(out_apk)):
            status = "Success" if not best_effort else "Best effort (some patches failed on this Brave version)"
            release_notes += f"## {variant['output_name']}\n- Brave version: `{tag}`\n- Patch bundle: `{dh6k_tag}`\n- Patches applied: {', '.join(enable)}\n"
            if skipped:
                release_notes += f"- Not in bundle (skipped): {', '.join(skipped)}\n"
            release_notes += f"- Status: {status}\n\n"
        else:
            release_notes += f"## {variant['output_name']}\n- Status: Failed\n\n"

    with open('release_notes.md', 'w') as f:
        f.write(release_notes)

if __name__ == "__main__":
    main()