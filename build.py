import os
import yaml
import json
import subprocess
import requests
import shutil

def get_latest_cli_jar():
    api_url = "https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest"
    release = requests.get(api_url).json()
    for asset in release.get("assets", []):
        if asset["name"].endswith("-all.jar"):
            url = asset["browser_download_url"]
            print(f"Downloading CLI: {asset['name']}")
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open("build/cli.jar", 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return asset["name"]
    raise Exception("Could not find morphe-desktop CLI jar")

def download_file(url, dest):
    print(f"Downloading {url} to {dest}...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def get_github_release(repo, release_type):
    api_url = f"https://api.github.com/repos/{repo}/releases"
    releases = requests.get(api_url).json()
    
    for release in releases:
        tag = release.get("tag_name", "").lower()
        name = release.get("name", "").lower()
        is_prerelease = release.get("prerelease", False)

        if release_type == "stable" and not is_prerelease:
            target = True
        elif release_type == "nightly" and is_prerelease and ("nightly" in tag or "nightly" in name):
            target = True
        elif release_type == "beta" and is_prerelease and ("beta" in tag or "beta" in name):
            target = True
        else:
            target = False

        if target:
            for asset in release.get("assets", []):
                if "arm64" in asset["name"].lower() and "universal" in asset["name"].lower():
                    return asset["browser_download_url"], release["tag_name"]
            for asset in release.get("assets", []):
                if asset["name"].endswith(".apk") and "arm64" in asset["name"].lower():
                    return asset["browser_download_url"], release["tag_name"]
                    
    return None, None

def main():
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    if os.path.exists('build'): shutil.rmtree('build')
    if os.path.exists('bundles'): shutil.rmtree('bundles')
    
    os.makedirs('build', exist_ok=True)
    os.makedirs('bundles', exist_ok=True)

    get_latest_cli_jar()

    # Download dh6k patches (Includes Pre-releases!)
    try:
        api_url = "https://api.github.com/repos/dh6k/morphe-patches/releases"
        releases = requests.get(api_url).json()
        downloaded = False
        for release in releases:
            if release.get("draft"):
                continue
            for asset in release.get("assets", []):
                if asset["name"].endswith(".mpp"):
                    download_file(asset["browser_download_url"], "bundles/dh6k.mpp")
                    print(f"Downloaded dh6k version: {release.get('tag_name')} (Prerelease: {release.get('prerelease')})")
                    downloaded = True
                    break
            if downloaded:
                break
        if not downloaded:
            raise Exception("No .mpp file found in dh6k releases")
    except Exception as e:
        print(f"Warning: Failed to download dh6k patches: {e}")

    release_notes = "# Morphe AutoBuilds Release\n\n"
    
    try:
        cli_version = subprocess.check_output(["java", "-jar", "build/cli.jar", "--version"]).decode().strip()
    except:
        cli_version = "Unknown"

    for variant in config['variants']:
        print(f"\n--- Building {variant['id']} ---")
        apk_url, tag = get_github_release("brave/brave-browser", variant['type'])
        
        if not apk_url:
            print(f"Could not find APK for {variant['type']}")
            continue

        apk_path = f"build/{variant['id']}_base.apk"
        download_file(apk_url, apk_path)

        out_apk = f"build/{variant['output_name']}-{tag}-patched.apk"
        
        included_patches = ["Brave origin", "Change app icon", "Disable analytics"]
        
        if variant.get('app_name'):
            included_patches.append("Change app name")
            
        if variant.get('clone_package'):
            included_patches.append("Clone app")
            
        # Build CLI Command
        cmd = [
            "java", "-jar", "build/cli.jar", "patch",
            "-p", "bundles/dh6k.mpp",
            "-o", out_apk,
            "--continue-on-error",
            apk_path
        ]
        
        # Enable selected patches
        for p in included_patches:
            cmd.extend(["-e", p])
            
        # Pass Options Directly via -O key=value
        cmd.extend(["-O", "customIcon=assets/isoamoledbraveicon.png"])
        
        if variant.get('app_name'):
            cmd.extend(["-O", f"appName={variant['app_name']}"])
            
        if variant.get('clone_package'):
            cmd.extend(["-O", f"packageName={variant['clone_package']}"])
            
        print("Running:", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True)
            release_notes += f"## {variant['output_name']}\n- Tag: `{tag}`\n- CLI Version: `{cli_version}`\n- Status: Success\n\n"
        except subprocess.CalledProcessError as e:
            print(f"Failed to build {variant['id']}")
            release_notes += f"## {variant['output_name']}\n- Status: Failed\n\n"

    with open('release_notes.md', 'w') as f:
        f.write(release_notes)

if __name__ == "__main__":
    main()