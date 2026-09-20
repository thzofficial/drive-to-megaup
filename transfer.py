import os
import glob
import json
import time
import subprocess
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

MEGAUP_KEY1 = os.environ.get("MEGAUP_KEY1")
MEGAUP_KEY2 = os.environ.get("MEGAUP_KEY2")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "ShareDrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB

# MegaUp Auth Session State
AUTH_STATE = {
    "access_token": None,
    "account_id": None
}

folder_cache = {}

def get_auth_token():
    """MegaUp YetiShare v2 API: Authorize with key1 and key2"""
    url = "https://megaup.net/api/v2/authorize"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    data = {
        "key1": MEGAUP_KEY1,
        "key2": MEGAUP_KEY2
    }
    try:
        r = requests.post(url, data=data, headers=headers, timeout=30)
        res = r.json()
        if res.get("_status") == "success":
            AUTH_STATE["access_token"] = res["data"]["access_token"]
            AUTH_STATE["account_id"] = str(res["data"]["account_id"])
            print(f"[+] MegaUp API Authorized Successfully! (Account ID: {AUTH_STATE['account_id']})", flush=True)
            return True
        else:
            print(f"[-] MegaUp Auth Failed: {res}", flush=True)
            return False
    except Exception as e:
        print(f"[-] MegaUp Auth Request Exception: {e}", flush=True)
        return False

def get_or_create_megaup_folder(folder_name, parent_id):
    if not folder_name:
        return parent_id

    cache_key = f"{parent_id}/{folder_name}"
    if cache_key in folder_cache:
        return folder_cache[cache_key]

    url = "https://megaup.net/api/v2/folder/create"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    data = {
        "access_token": AUTH_STATE["access_token"],
        "account_id": AUTH_STATE["account_id"],
        "parent_id": str(parent_id),
        "folder_name": folder_name
    }

    try:
        r = requests.post(url, data=data, headers=headers, timeout=30)
        res = r.json()
        if res.get("_status") == "success":
            new_id = str(res["data"]["folder_id"])
            folder_cache[cache_key] = new_id
            print(f"  [+] Created MegaUp folder: '{folder_name}' (ID: {new_id})", flush=True)
            return new_id
    except Exception as e:
        print(f"  [-] Folder create warning: {e}", flush=True)

    return parent_id

def upload_file_to_megaup(file_path, folder_id, retries=3):
    url = "https://megaup.net/api/v2/file/upload"
    file_name = os.path.basename(file_path)

    for attempt in range(1, retries + 1):
        try:
            with open(file_path, "rb") as f:
                # YetiShare API standard field: upload_file
                encoder = MultipartEncoder(
                    fields={
                        "access_token": AUTH_STATE["access_token"],
                        "account_id": AUTH_STATE["account_id"],
                        "folder_id": str(folder_id),
                        "upload_file": (file_name, f, "application/octet-stream")
                    }
                )

                last_percent = [-10]
                total_mb = encoder.len / (1024 * 1024)

                def progress_callback(monitor):
                    current_percent = int((monitor.bytes_read / monitor.len) * 100)
                    if current_percent >= last_percent[0] + 10 or current_percent == 100:
                        uploaded_mb = monitor.bytes_read / (1024 * 1024)
                        print(f"    -> Uploading: {current_percent}% ({uploaded_mb:.1f}/{total_mb:.1f} MB)", flush=True)
                        last_percent[0] = current_percent

                monitor = MultipartEncoderMonitor(encoder, progress_callback)
                headers = {
                    "Content-Type": monitor.content_type,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }

                r = requests.post(url, data=monitor, headers=headers, timeout=3600)

            try:
                res = r.json()
                if res.get("_status") == "success":
                    return res
                else:
                    print(f"    [-] API error response: {res}", flush=True)
            except Exception:
                print(f"    [-] Non-JSON response (Status {r.status_code}): {r.text[:200]}", flush=True)

        except Exception as err:
            print(f"    [!] Upload attempt {attempt}/{retries} failed ({err}). Retrying in 10s...", flush=True)
            time.sleep(10)

    return {"_status": "error"}

# ၁။ စတင်သည်နှင့် MegaUp Auth အရင်လုပ်ဆောင်ခြင်း
print("[*] Authenticating with MegaUp API...", flush=True)
if not get_auth_token():
    print("[-] Aborting due to authentication failure. Please check MEGAUP_KEY1 and MEGAUP_KEY2 secrets.", flush=True)
    exit(1)

# ၂။ Shared Drive ရှိ ဖိုင်များကို စစ်ဆေးခြင်း
print(f"[*] Scanning folder: {TARGET_REMOTE_FOLDER}...", flush=True)
cmd = ["rclone", "lsjson", "-R", "--files-only", TARGET_REMOTE_FOLDER]
res = subprocess.run(cmd, capture_output=True, text=True)

if res.returncode != 0:
    print("[-] Rclone Listing Error:", res.stderr, flush=True)
    exit(1)

try:
    files_metadata = json.loads(res.stdout)
except json.JSONDecodeError:
    print("[-] Failed to parse file list.", flush=True)
    exit(1)

print(f"[+] Found {len(files_metadata)} files in Backup.", flush=True)

if not files_metadata:
    print("[*] Backup folder is empty. Exiting...", flush=True)
    exit(0)

for idx, file_info in enumerate(files_metadata, 1):
    rel_path = file_info.get("Path")
    file_size = file_info.get("Size", 0)
    filename = os.path.basename(rel_path)
    file_size_gb = file_size / (1024 ** 3)

    sub_dir = os.path.dirname(rel_path)
    target_folder_id = ROOT_FOLDER_ID

    if sub_dir:
        current_parent = ROOT_FOLDER_ID
        for folder_part in sub_dir.split("/"):
            if folder_part:
                current_parent = get_or_create_megaup_folder(folder_part, current_parent)
        target_folder_id = current_parent

    local_path = os.path.join(TEMP_DIR, filename)
    remote_file = f"{TARGET_REMOTE_FOLDER}/{rel_path}"

    print(f"\n[{idx}/{len(files_metadata)}] Processing: {rel_path} ({file_size_gb:.2f} GB)", flush=True)

    print("  -> Downloading from Shared Drive...", flush=True)
    dl = subprocess.run(["rclone", "copyto", "-P", remote_file, local_path])
    if dl.returncode != 0:
        print("  [-] Download failed. Skipping file...", flush=True)
        continue

    upload_success_all_parts = True

    if file_size > SPLIT_SIZE_BYTES:
        print(f"  [!] File size > 4GB. Splitting into 4GB chunks...", flush=True)
        split_prefix = local_path + ".part"
        subprocess.run(["split", "-b", "4G", "-d", local_path, split_prefix], check=True)

        if os.path.exists(local_path):
            os.remove(local_path)

        part_files = sorted(glob.glob(f"{split_prefix}*"))
        print(f"  -> Generated {len(part_files)} parts. Uploading each part...", flush=True)

        for part in part_files:
            part_name = os.path.basename(part)
            print(f"    -> Uploading chunk: {part_name}...", flush=True)
            up_res = upload_file_to_megaup(part, target_folder_id)

            if up_res.get("_status") == "success":
                print(f"    [+] Chunk uploaded: {up_res.get('data', {}).get('url')}", flush=True)
                os.remove(part)
            else:
                upload_success_all_parts = False
                break
    else:
        print(f"  -> Uploading to MegaUp Folder ID: {target_folder_id}...", flush=True)
        up_res = upload_file_to_megaup(local_path, target_folder_id)
        if up_res.get("_status") == "success":
            print(f"  [+] Uploaded Successfully! URL: {up_res.get('data', {}).get('url')}", flush=True)
        else:
            upload_success_all_parts = False

        if os.path.exists(local_path):
            os.remove(local_path)

    if upload_success_all_parts:
        print("  -> Deleting original file from Google Shared Drive...", flush=True)
        del_cmd = subprocess.run(["rclone", "deletefile", remote_file])
        if del_cmd.returncode == 0:
            print("  [+] Successfully cleaned from Shared Drive!", flush=True)
        else:
            print("  [-] Warning: Failed to delete from Shared Drive.", flush=True)
    else:
        print("  [!] Keeping file in Shared Drive due to upload failure.", flush=True)

print("\n[+] All pending files processed!", flush=True)
