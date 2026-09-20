import os
import subprocess
import requests

MEGAUP_KEY1 = os.environ.get("MEGAUP_KEY1")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "sharedrive:Music")

TEMP_DIR = "/tmp/transfer_cache"
HISTORY_FILE = "uploaded.txt"
os.makedirs(TEMP_DIR, exist_ok=True)

# ယခင်တင်ပြီးသား ဖိုင်မှတ်တမ်းများကို ဖတ်ယူခြင်း
uploaded_history = set()
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        uploaded_history = set(line.strip() for line in f if line.strip())

def upload_file_to_megaup(file_path, folder_id):
    url = "https://megaup.net/api/v2/file/upload"
    data = {"key1": MEGAUP_KEY1, "folder_id": str(folder_id)}
    with open(file_path, "rb") as f:
        r = requests.post(url, data=data, files={"file": f}, timeout=1800)
    try:
        return r.json()
    except Exception:
        return {"_status": "error", "raw": r.text}

print(f"[*] Checking Shared Drive: {RCLONE_REMOTE}...")
cmd = ["rclone", "lsf", "-R", "--files-only", RCLONE_REMOTE]
res = subprocess.run(cmd, capture_output=True, text=True)

if res.returncode != 0:
    print("[-] Rclone Error:", res.stderr)
    exit(1)

all_remote_files = [f.strip() for f in res.stdout.splitlines() if f.strip()]

# မတင်ရသေးသော ဖိုင်အသစ်များကိုသာ စစ်ထုတ်ခြင်း
new_files = [f for f in all_remote_files if f not in uploaded_history]
print(f"[+] Total files in Drive: {len(all_remote_files)} | New files to upload: {len(new_files)}")

if not new_files:
    print("[*] No new files found. Exiting to save GitHub Action minutes.")
    exit(0)

for idx, rel_path in enumerate(new_files, 1):
    filename = os.path.basename(rel_path)
    local_path = os.path.join(TEMP_DIR, filename)
    remote_source = f"{RCLONE_REMOTE}/{rel_path}"

    print(f"\n[{idx}/{len(new_files)}] Transferring: {rel_path}")
    
    # 1. Download to local tmp
    dl = subprocess.run(["rclone", "copyto", remote_source, local_path])
    if dl.returncode != 0:
        print("  [-] Download failed, skipping.")
        continue

    # 2. Upload to Megaup
    up_res = upload_file_to_megaup(local_path, ROOT_FOLDER_ID)
    
    if up_res.get("_status") == "success":
        print(f"  [+] Success! Link: {up_res.get('data', {}).get('url')}")
        # အောင်မြင်ပါက မှတ်တမ်းထဲ ထည့်သွင်းခြင်း
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{rel_path}\n")
    else:
        print(f"  [-] Failed: {up_res}")

    # 3. Disk ရှင်းထုတ်ခြင်း
    if os.path.exists(local_path):
        os.remove(local_path)

print("\n[+] Done!")
