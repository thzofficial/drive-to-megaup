import os
import glob
import json
import subprocess
import requests

MEGAUP_KEY1 = os.environ.get("MEGAUP_KEY1")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "sharedrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

# 4GB Limit (MegaUp ၏ 5GB limit ထက် လုံခြုံစွာ လျှော့ထားခြင်း)
SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB

def upload_file_to_megaup(file_path, folder_id):
    url = "https://megaup.net/api/v2/file/upload"
    data = {"key1": MEGAUP_KEY1, "folder_id": str(folder_id)}
    with open(file_path, "rb") as f:
        r = requests.post(url, data=data, files={"file": f}, timeout=3600)
    try:
        return r.json()
    except Exception:
        return {"_status": "error", "raw": r.text}

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
    local_path = os.path.join(TEMP_DIR, filename)
    remote_file = f"{TARGET_REMOTE_FOLDER}/{rel_path}"

    print(f"\n[{idx}/{len(files_metadata)}] Processing: {rel_path} ({file_size_gb:.2f} GB)")

    # ၁။ Google Shared Drive မှ VM ထဲသို့ ဒေါင်းလုဒ်ဆွဲခြင်း
    print("  -> Downloading from Shared Drive...")
    dl = subprocess.run(["rclone", "copyto", remote_file, local_path])
    if dl.returncode != 0:
        print("  [-] Download failed. Skipping file...")
        continue

    # ၂။ ဖိုင်ကို Split ခွဲရန် လို/မလို စစ်ဆေးခြင်း
    upload_success_all_parts = True

    if file_size > SPLIT_SIZE_BYTES:
        print(f"  [!] File size > 4GB. Splitting into 4GB chunks...")
        # 4GB စီ အပိုင်းခွဲခြင်း (.part00, .part01 ...)
        split_prefix = local_path + ".part"
        subprocess.run(["split", "-b", "4G", "-d", local_path, split_prefix], check=True)

        # နေရာလွတ်စေရန် မူရင်းဖိုင်ကြီးကို ချက်ချင်းဖျက်ပစ်ခြင်း
        if os.path.exists(local_path):
            os.remove(local_path)

        # Split အပိုင်းများကို ရှာဖွေပြီး အစဉ်လိုက် တင်ခြင်း
        part_files = sorted(glob.glob(f"{split_prefix}*"))
        print(f"  -> Generated {len(part_files)} parts. Uploading each part...")

        for part in part_files:
            part_name = os.path.basename(part)
            print(f"    -> Uploading chunk: {part_name}...")
            up_res = upload_file_to_megaup(part, ROOT_FOLDER_ID)

            if up_res.get("_status") == "success":
                print(f"    [+] Chunk uploaded: {up_res.get('data', {}).get('url')}")
                os.remove(part)  # တင်ပြီးတာနဲ့ chunk ကို ချက်ချင်းဖျက်
            else:
                print(f"    [-] Chunk upload failed: {up_res}")
                upload_success_all_parts = False
                break
    else:
        # 4GB အောက်ဖိုင်များ (Split ခွဲစရာမလိုဘဲ တိုက်ရိုက်တင်ခြင်း)
        print(f"  -> Uploading full file...")
        up_res = upload_file_to_megaup(local_path, ROOT_FOLDER_ID)
        if up_res.get("_status") == "success":
            print(f"  [+] Uploaded Successfully! URL: {up_res.get('data', {}).get('url')}")
        else:
            print(f"  [-] Upload Failed: {up_res}")
            upload_success_all_parts = False

        if os.path.exists(local_path):
            os.remove(local_path)

    # ၃။ အပိုင်းအားလုံး (သို့) မူရင်းဖိုင် MegaUp သို့ အောင်မြင်စွာ တင်ပြီးမှသာ Drive ထဲက ဖျက်ခြင်း
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
