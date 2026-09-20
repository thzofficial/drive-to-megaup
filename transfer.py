import os
import glob
import json
import time
import subprocess
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

MEGAUP_KEY1 = os.environ.get("MEGAUP_KEY1")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "ShareDrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB

# MegaUp ပေါ်တွင် ဆောက်ပြီးသား Folder ID များကို Cache လုပ်ထားမည့် dictionary
folder_cache = {}

def get_or_create_megaup_folder(folder_name, parent_id):
    """MegaUp ပေါ်တွင် Folder ရှိမရှိ စစ်ပြီး မရှိပါက အသစ်ဆောက်ပေးခြင်း"""
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
        "key1": MEGAUP_KEY1,
        "parent_id": str(parent_id),
        "folder_name": folder_name
    }

    try:
        r = requests.post(url, data=data, headers=headers, timeout=30)
        res = r.json()
        if res.get("_status") == "success":
            new_id = str(res["data"]["folder_id"])
            folder_cache[cache_key] = new_id
            print(f"  [+] Created MegaUp folder: '{folder_name}' (ID: {new_id})")
            return new_id
    except Exception as e:
        print(f"  [-] Folder create warning: {e}")

    return parent_id

def upload_file_to_megaup(file_path, folder_id, retries=3):
    """Streaming Upload ဖြင့် SSL EOF Error ကာကွယ်ခြင်း"""
    url = "https://megaup.net/api/v2/file/upload"
    file_name = os.path.basename(file_path)

    for attempt in range(1, retries + 1):
        try:
            with open(file_path, "rb") as f:
                m = MultipartEncoder(
                    fields={
                        "key1": MEGAUP_KEY1,
                        "folder_id": str(folder_id),
                        "file": (file_name, f, "application/octet-stream")
                    }
                )
                headers = {
                    "Content-Type": m.content_type,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                r = requests.post(url, data=m, headers=headers, timeout=3600)

            try:
                res = r.json()
                if res.get("_status") == "success":
                    return res
                else:
                    print(f"    [-] API response error: {res}")
            except Exception:
                print(f"    [-] Non-JSON response: {r.text[:200]}")

        except Exception as err:
            print(f"    [!] Upload attempt {attempt}/{retries} failed ({err}). Retrying in 10s...")
            time.sleep(10)

    return {"_status": "error"}

print(f"[*] Scanning folder: {TARGET_REMOTE_FOLDER}...")
cmd = ["rclone", "lsjson", "-R", "--files-only", TARGET_REMOTE_FOLDER]
res = subprocess.run(cmd, capture_output=True, text=True)

if res.returncode != 0:
    print("[-] Rclone Listing Error:", res.stderr)
    exit(1)

try:
    files_metadata = json.loads(res.stdout)
except json.JSONDecodeError:
    print("[-] Failed to parse file list.")
    exit(1)

print(f"[+] Found {len(files_metadata)} files in Backup.")

if not files_metadata:
    print("[*] Backup folder is empty. Exiting...")
    exit(0)

for idx, file_info in enumerate(files_metadata, 1):
    rel_path = file_info.get("Path")
    file_size = file_info.get("Size", 0)
    filename = os.path.basename(rel_path)
    file_size_gb = file_size / (1024 ** 3)

    # Subfolder ရှိပါက Folder Name ခွဲထုတ်ခြင်း (ဥပမာ: "HANNE BOEL - UNPLUGGED (DSF64)")
    sub_dir = os.path.dirname(rel_path)
    target_folder_id = ROOT_FOLDER_ID

    if sub_dir:
        # Folder အဆင့်ဆင့် ရှိပါက MegaUp တွင် အဆင့်ဆင့် ဆောက်ပေးခြင်း
        current_parent = ROOT_FOLDER_ID
        for folder_part in sub_dir.split("/"):
            if folder_part:
                current_parent = get_or_create_megaup_folder(folder_part, current_parent)
        target_folder_id = current_parent

    local_path = os.path.join(TEMP_DIR, filename)
    remote_file = f"{TARGET_REMOTE_FOLDER}/{rel_path}"

    print(f"\n[{idx}/{len(files_metadata)}] Processing: {rel_path} ({file_size_gb:.2f} GB)")

    # ၁။ Google Shared Drive မှ VM ထဲ ဒေါင်းယူခြင်း
    print("  -> Downloading from Shared Drive...")
    dl = subprocess.run(["rclone", "copyto", remote_file, local_path])
    if dl.returncode != 0:
        print("  [-] Download failed. Skipping file...")
        continue

    # ၂။ 4GB ထက်ကြီးပါက Split လုပ်ပြီး တင်ခြင်း
    upload_success_all_parts = True

    if file_size > SPLIT_SIZE_BYTES:
        print(f"  [!] File size > 4GB. Splitting into 4GB chunks...")
        split_prefix = local_path + ".part"
        subprocess.run(["split", "-b", "4G", "-d", local_path, split_prefix], check=True)

        if os.path.exists(local_path):
            os.remove(local_path)

        part_files = sorted(glob.glob(f"{split_prefix}*"))
        print(f"  -> Generated {len(part_files)} parts. Uploading each part...")

        for part in part_files:
            part_name = os.path.basename(part)
            print(f"    -> Uploading chunk: {part_name} to MegaUp Folder ID: {target_folder_id}...")
            up_res = upload_file_to_megaup(part, target_folder_id)

            if up_res.get("_status") == "success":
                print(f"    [+] Chunk uploaded: {up_res.get('data', {}).get('url')}")
                os.remove(part)
            else:
                upload_success_all_parts = False
                break
    else:
        print(f"  -> Uploading to MegaUp Folder ID: {target_folder_id}...")
        up_res = upload_file_to_megaup(local_path, target_folder_id)
        if up_res.get("_status") == "success":
            print(f"  [+] Uploaded Successfully! URL: {up_res.get('data', {}).get('url')}")
        else:
            upload_success_all_parts = False

        if os.path.exists(local_path):
            os.remove(local_path)

    # ၃။ အောင်မြင်စွာ တင်ပြီးမှသာ Shared Drive မှ မူရင်းဖိုင် ဖျက်ခြင်း
    if upload_success_all_parts:
        print("  -> Deleting original file from Google Shared Drive...")
        del_cmd = subprocess.run(["rclone", "deletefile", remote_file])
        if del_cmd.returncode == 0:
            print("  [+] Successfully cleaned from Shared Drive!")
        else:
            print("  [-] Warning: Failed to delete from Shared Drive.")
    else:
        print("  [!] Keeping file in Shared Drive due to upload failure.")

print("\n[+] All pending files processed!")
