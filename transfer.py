import os
import subprocess
import requests

MEGAUP_KEY1 = os.environ.get("MEGAUP_KEY1")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "sharedrive")

# ပစ်မှတ်ထားမည့် ဖိုဒါလမ်းကြောင်း (Shared Drive အောက်က Backup)
TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

def upload_file_to_megaup(file_path, folder_id):
    url = "https://megaup.net/api/v2/file/upload"
    data = {"key1": MEGAUP_KEY1, "folder_id": str(folder_id)}
    with open(file_path, "rb") as f:
        r = requests.post(url, data=data, files={"file": f}, timeout=1800)
    try:
        return r.json()
    except Exception:
        return {"_status": "error", "raw": r.text}

print(f"[*] Scanning folder: {TARGET_REMOTE_FOLDER}...")

# Backup ဖိုဒါထဲရှိ ဖိုင်များကိုသာ စစ်ထုတ်ခြင်း
cmd = ["rclone", "lsf", "-R", "--files-only", TARGET_REMOTE_FOLDER]
res = subprocess.run(cmd, capture_output=True, text=True)

if res.returncode != 0:
    print("[-] Rclone Listing Error:", res.stderr)
    exit(1)

files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
print(f"[+] Files found in Backup: {len(files)}")

if not files:
    print("[*] Backup folder is empty. Exiting...")
    exit(0)

for idx, rel_path in enumerate(files, 1):
    filename = os.path.basename(rel_path)
    local_path = os.path.join(TEMP_DIR, filename)
    remote_file = f"{TARGET_REMOTE_FOLDER}/{rel_path}"

    print(f"\n[{idx}/{len(files)}] Processing: {rel_path}")

    # ၁။ Shared Drive မှ ယာယီဒေါင်းလုဒ်ဆွဲခြင်း
    print("  -> Downloading to temp...")
    dl = subprocess.run(["rclone", "copyto", remote_file, local_path])
    if dl.returncode != 0:
        print("  [-] Download failed. Skipping file...")
        continue

    # ၂။ MegaUp သို့ တင်ခြင်း
    print(f"  -> Uploading to MegaUp (Folder: {ROOT_FOLDER_ID})...")
    up_res = upload_file_to_megaup(local_path, ROOT_FOLDER_ID)

    # ၃။ MegaUp သို့ တင်တာ အောင်မြင်မှသာ Shared Drive နှင့် Local မှ ဖျက်ခြင်း
    if up_res.get("_status") == "success":
        print(f"  [+] Uploaded Successfully! URL: {up_res.get('data', {}).get('url')}")
        
        # Google Shared Drive ထဲက မူရင်းဖိုင်ကို ဖျက်ထုတ်ခြင်း
        print("  -> Deleting original file from Google Shared Drive...")
        del_cmd = subprocess.run(["rclone", "deletefile", remote_file])
        if del_cmd.returncode == 0:
            print("  [+] Cleaned from Shared Drive!")
        else:
            print("  [-] Warning: Failed to delete from Shared Drive.")
    else:
        print(f"  [-] Upload Failed! Keeping file in Shared Drive: {up_res}")

    # Local disk ရှင်းထုတ်ခြင်း
    if os.path.exists(local_path):
        os.remove(local_path)

print("\n[+] All pending backup files processed and cleaned up!")
