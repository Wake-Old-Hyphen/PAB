import os
import yaml
import json
import subprocess
import requests
import glob
import re

def get_github_release(repo, release_type):
    api_url = f"https://api.github.com/repos/{repo}/releases"
    releases = requests.get(api_url).json()
    
    for release in releases:
        tag = release.get("tag_name", "").lower()
        name = release.get("name", "").lower()
        is_prerelease = release.get("prerelease", False)

        # Match the correct release type
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
                # Look for the universal arm64 apk
                if "arm64" in asset["name"].lower() and "universal" in asset["name"].lower():
                    return asset["browser_download_url"], release["tag_name"]
            # Fallback if exact name isn't found
            for asset in release.get("assets", []):
                if asset["name"].endswith(".apk") and "arm64" in asset["name"].lower():
                    return asset["browser_download_url"], release["tag_name"]
                    
    return None, None

def download_file(url, dest):
    print(f"Downloading {url} to {dest}...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def main():
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    os.makedirs('build', exist_ok=True)
    os.makedirs('bundles', exist_ok=True)

    # Download CLI and Patches
    # NOTE: You may need to update these URLs to the exact Morphe CLI and bundle URLs
    cli_url = "https://github.com/ReVanced/revanced-cli/releases/latest/download/revanced-cli.jar"
    patches_url = "https://github.com/dh6k/morphe-patches/releases/latest/download/morphe-patches.jar"
    integrations_url = "https://github.com/dh6k/morphe-patches/releases/latest/download/morphe-integrations.apk"
    
    download_file(cli_url, "build/cli.jar")
    download_file(patches_url, "build/patches.jar")
    download_file(integrations_url, "build/integrations.apk")

    release_notes = "# Morphe AutoBuilds Release\n\n"

    for variant in config['variants']:
        print(f"\n--- Building {variant['id']} ---")
        apk_url, tag = get_github_release("brave/brave-browser", variant['type'])
        
        if not apk_url:
            print(f"Could not find APK for {variant['type']}")
            continue

        apk_path = f"build/{variant['id']}_base.apk"
        download_file(apk_url, apk_path)

        # Generate Morphe/ReVanced options.json
        options = [
            {"patchName": "Brave origin", "options": {}},
            {"patchName": "Change app icon", "options": {"iconPath": "assets/isoamoledbraveicon.png"}},
            {"patchName": "Disable analytics", "options": {}}
        ]

        if variant['app_name']:
            options.append({"patchName": "Change app name", "options": {"appName": variant['app_name']}})
            
        if variant['clone_package']:
            options.append({"patchName": "Clone app", "options": {"packageName": variant['clone_package']}})

        options_path = f"build/{variant['id']}_options.json"
        with open(options_path, 'w') as f:
            json.dump(options, f)

        out_apk = f"build/{variant['output_name']}-{tag}-patched.apk"
        
        cmd = [
            "java", "-jar", "build/cli.jar", "patch",
            "-a", apk_path,
            "-b", "build/patches.jar",
            "-m", "build/integrations.apk",
            "-o", out_apk,
            "--options", options_path
        ]
        
        print("Running:", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True)
            release_notes += f"## {variant['output_name']}\n- Tag: `{tag}`\n- Status: Success\n\n"
        except subprocess.CalledProcessError as e:
            print(f"Failed to build {variant['id']}")
            release_notes += f"## {variant['output_name']}\n- Status: Failed\n\n"

    with open('release_notes.md', 'w') as f:
        f.write(release_notes)

if __name__ == "__main__":
    main()