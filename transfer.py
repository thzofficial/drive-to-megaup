import os
import re
import glob
import json
import time
import subprocess
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

COOKIE_STRING = os.environ.get("MEGAUP_COOKIE", "")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "ShareDrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB

# Default YetiShare browser headers
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
    """MegaUp ပင်မစာမျက်နှာမှ upload handler URL နှင့် session tokens များကို ရှာဖွေခြင်း"""
    print("[*] Fetching MegaUp Web Upload Session...", flush=True)
    try:
        r = session.get("https://megaup.net/", timeout=30)
        html = r.text

        # YetiShare upload url format ရှာဖွေခြင်း
        upload_url_match = re.search(r'uploadUrl\s*=\s*[\'"]([^\'"]+)[\'"]', html)
        upload_url = upload_url_match.group(1) if upload_url_match else "https://megaup.net/core/page/ajax/file_upload_handler.ajax.php"

        # cTracker သို့မဟုတ် session token ရှာဖွေခြင်း
        c_tracker_match = re.search(r'cTracker\s*=\s*[\'"]([^\'"]+)[\'"]', html)
        c_tracker = c_tracker_match.group(1) if c_tracker_match else ""

        print(f"[+] Web Upload Endpoint: {upload_url}", flush=True)
        return upload_url, c_tracker
    except Exception as e:
        print(f"[-] Session Fetch Error: {e}", flush=True)
        return "https://megaup.net/core/page/ajax/file_upload_handler.ajax.php", ""

UPLOAD_ENDPOINT, CTRACKER = parse_upload_params()

def upload_file_web(file_path, folder_id, retries=3):
    file_name = os.path.basename(file_path)

    for attempt in range(1, retries + 1):
        try:
            with open(file_path, "rb") as f:
                fields = {
                    "folder_id": str(folder_id),
                    "cTracker": CTRACKER,
                    "files[]": (file_name, f, "application/octet-stream")
                }
                encoder = MultipartEncoder(fields=fields)

                last_percent = [-10]
                total_mb = encoder.len / (1024 * 1024)

                def progress_callback(monitor):
                    current_percent = int((monitor.bytes_read / monitor.len) * 100)
                    if current_percent >= last_percent[0] + 10 or current_percent == 100:
                        uploaded_mb = monitor.bytes_read / (1024 * 1024)
                        print(f"    -> Uploading: {current_percent}% ({uploaded_mb:.1f}/{total_mb:.1f} MB)", flush=True)
                        last_percent[0] = current_percent

                monitor = MultipartEncoderMonitor(encoder, progress_callback)
                headers = dict(COMMON_HEADERS)
                headers["Content-Type"] = monitor.content_type

                r = session.post(UPLOAD_ENDPOINT, data=monitor, headers=headers, timeout=3600)

            try:
                res = r.json()
                # YetiShare returns an array of uploaded files [{name, size, url, error...}]
                if isinstance(res, list) and len(res) > 0:
                    first = res[0]
                    if "error" not in first:
                        return {"_status": "success", "data": first}
                    else:
                        print(f"    [-] MegaUp Upload Error: {first.get('error')}", flush=True)
                elif isinstance(res, dict) and res.get("_status") == "success":
                    return res
                else:
                    print(f"    [-] Response: {res}", flush=True)
            except Exception:
                print(f"    [-] Non-JSON response ({r.status_code}): {r.text[:250]}", flush=True)

        except Exception as err:
            print(f"    [!] Attempt {attempt}/{retries} failed ({err}). Retrying in 10s...", flush=True)
            time.sleep(10)

    return {"_status": "error"}

# ၁။ Google Shared Drive ရှိ ဖိုင်များကို စစ်ဆေးခြင်း
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
            print(f"    -> Uploading chunk: {part_name}...", flush=True)
            up_res = upload_file_web(part, ROOT_FOLDER_ID)

            if up_res.get("_status") == "success":
                print(f"    [+] Chunk uploaded successfully!", flush=True)
                os.remove(part)
            else:
                upload_success_all_parts = False
                break
    else:
        print(f"  -> Uploading full file to MegaUp Folder ID: {ROOT_FOLDER_ID}...", flush=True)
        up_res = upload_file_web(local_path, ROOT_FOLDER_ID)
        if up_res.get("_status") == "success":
            print(f"  [+] Uploaded Successfully!", flush=True)
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
