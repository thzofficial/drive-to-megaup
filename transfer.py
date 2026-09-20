import os
import re
import glob
import json
import time
import math
import subprocess
import requests

COOKIE_STRING = os.environ.get("MEGAUP_COOKIE", "")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "ShareDrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB chunks (Cloudflare limit ကျော်လွှားရန်)

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://megaup.net",
    "Referer": "https://megaup.net/",
    "Cookie": COOKIE_STRING
}

session = requests.Session()
session.headers.update(COMMON_HEADERS)

def parse_upload_params():
    print("[*] Fetching MegaUp Web Upload Session...", flush=True)
    try:
        r = session.get("https://megaup.net/", timeout=30)
        html = r.text

        upload_url_match = re.search(r'uploadUrl\s*=\s*[\'"]([^\'"]+)[\'"]', html)
        upload_url = upload_url_match.group(1) if upload_url_match else "https://megaup.net/core/page/ajax/file_upload_handler.ajax.php"

        c_tracker_match = re.search(r'cTracker\s*=\s*[\'"]([^\'"]+)[\'"]', html)
        c_tracker = c_tracker_match.group(1) if c_tracker_match else ""

        print(f"[+] Web Upload Endpoint: {upload_url}", flush=True)
        return upload_url, c_tracker
    except Exception as e:
        print(f"[-] Session Fetch Error: {e}", flush=True)
        return "https://megaup.net/core/page/ajax/file_upload_handler.ajax.php", ""

UPLOAD_ENDPOINT, CTRACKER = parse_upload_params()

def upload_file_chunked(file_path, folder_id, max_retries=3):
    """5MB Chunks စီ ခွဲပို့သည့် YetiShare Web Upload Flow (SSL EOF Error ကင်းစင်စေသည်)"""
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    total_chunks = math.ceil(file_size / CHUNK_SIZE)
    
    print(f"  -> Uploading '{file_name}' ({file_size / (1024*1024):.1f} MB) in {total_chunks} chunks...", flush=True)

    with open(file_path, "rb") as f:
        for chunk_idx in range(total_chunks):
            chunk_data = f.read(CHUNK_SIZE)
            start_byte = chunk_idx * CHUNK_SIZE
            end_byte = start_byte + len(chunk_data) - 1

            headers = dict(COMMON_HEADERS)
            # YetiShare / jQuery File Upload Standard Content-Range Header
            headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
            headers["Content-Disposition"] = f'attachment; filename="{file_name}"'

            fields = {
                "folder_id": str(folder_id),
                "cTracker": CTRACKER,
            }
            files = {
                "files[]": (file_name, chunk_data, "application/octet-stream")
            }

            # Retry logic per chunk
            success = False
            for attempt in range(1, max_retries + 1):
                try:
                    r = session.post(UPLOAD_ENDPOINT, data=fields, files=files, headers=headers, timeout=60)
                    if r.status_code == 200:
                        success = True
                        break
                    else:
                        print(f"    [!] Chunk {chunk_idx + 1} returned status {r.status_code}. Retrying...", flush=True)
                except Exception as err:
                    print(f"    [!] Chunk {chunk_idx + 1} attempt {attempt} failed: {err}. Retrying in 5s...", flush=True)
                    time.sleep(5)

            if not success:
                print(f"[-] Failed to upload chunk {chunk_idx + 1}/{total_chunks}.", flush=True)
                return {"_status": "error"}

            # Progress log
            percent = int(((chunk_idx + 1) / total_chunks) * 100)
            if percent % 10 == 0 or chunk_idx == total_chunks - 1:
                uploaded_mb = (end_byte + 1) / (1024 * 1024)
                print(f"    -> Progress: {percent}% ({uploaded_mb:.1f}/{file_size / (1024*1024):.1f} MB)", flush=True)

    return {"_status": "success"}

# --- Process Files ---
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
            up_res = upload_file_chunked(part, ROOT_FOLDER_ID)

            if up_res.get("_status") == "success":
                print(f"    [+] Chunk part '{part_name}' uploaded successfully!", flush=True)
                os.remove(part)
            else:
                upload_success_all_parts = False
                break
    else:
        up_res = upload_file_chunked(local_path, ROOT_FOLDER_ID)
        if up_res.get("_status") == "success":
            print(f"  [+] File '{filename}' Uploaded Successfully!", flush=True)
        else:
            upload_success_all_parts = False

        if os.path.exists(local_path):
            os.remove(local_path)

    # အောင်မြင်မှသာ Drive ထဲက ဖျက်ခြင်း
    if upload_success_all_parts:
        print("  -> Deleting original file from Google Shared Drive...", flush=True)
        del_cmd = subprocess.run(["rclone", "deletefile", remote_file])
        if del_cmd.returncode == 0:
            print("  [+] Cleaned from Shared Drive!", flush=True)
        else:
            print("  [-] Failed to delete from Shared Drive.", flush=True)
    else:
        print("  [!] Keeping file in Shared Drive due to upload failure.", flush=True)

print("\n[+] All pending backup files processed!", flush=True)
