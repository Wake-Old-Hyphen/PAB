import os
import yaml
import subprocess
import requests
import shutil

MAX_ATTEMPTS = 5  # how many older releases to try per channel

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

def pick_candidates(releases, channel):
    cands = []
    for r in releases:
        tag = (r.get("tag_name") or "")
        low = tag.lower() + (r.get("name") or "").lower()
        pre = r.get("prerelease", False)
        ok = False
        if channel == "stable" and not pre:
            ok = True
        elif channel == "nightly" and pre and "nightly" in low:
            ok = True
        elif channel == "beta" and pre and "beta" in low:
            ok = True
        if ok:
            for a in r.get("assets", []):
                n = a["name"].lower()
                if "arm64" in n and "universal" in n:
                    cands.append((tag, a["browser_download_url"]))
                    break
    return cands[:MAX_ATTEMPTS]

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

    # dh6k bundle (latest release, pre-releases included)
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

    # Find the newest compatible Brave version per channel
    channel_info = {}
    for channel in ["stable", "nightly", "beta"]:
        cands = pick_candidates(brave_releases, channel)
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
            # nothing worked: publish latest anyway, best effort
            download_file(cands[0][1], f"build/base_{channel}.apk")
            chosen_tag = cands[0][0]
            print(f"{channel}: no compatible version in last {MAX_ATTEMPTS}, using latest (best effort)")
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