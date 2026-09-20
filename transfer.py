import os
import re
import glob
import json
import time
import uuid
import pathlib
import unicodedata
import subprocess
import requests

MEGAUP_BASE = "https://megaup.net"
RAW_COOKIE = os.environ.get("MEGAUP_COOKIE", "")
ROOT_FOLDER_ID = os.environ.get("MEGAUP_FOLDER_ID", "63172")
REMOTE_NAME = os.environ.get("RCLONE_REMOTE", "ShareDrive")

TARGET_REMOTE_FOLDER = f"{REMOTE_NAME}:Backup"
TEMP_DIR = "/tmp/transfer_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

CHUNK_SIZE = 15 * 1024 * 1024  # 15MB chunks
SPLIT_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB

_RESOLVED_UPLOAD_URL = None
folder_cache = {}

def get_authenticated_session() -> requests.Session:
    """Cookie တစ်ကြောင်းတည်းမှ filehosting နှင့် cf_clearance ကို အလိုအလျောက် ခွဲယူခြင်း"""
    session = requests.Session()
    
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{MEGAUP_BASE}/",
        "Origin": MEGAUP_BASE,
        "Cookie": RAW_COOKIE.strip(),
    })
    return session

def resolve_upload_url(session: requests.Session) -> str:
    """Storage node ကို အလိုအလျောက် ရှာဖွေခြင်း"""
    global _RESOLVED_UPLOAD_URL
    if _RESOLVED_UPLOAD_URL:
        return _RESOLVED_UPLOAD_URL

    try:
        res = session.get(f"{MEGAUP_BASE}/", timeout=30)
        res.raise_for_status()
        match = re.search(r'https?://[a-zA-Z0-9_\-\.]+\.mupload\.store/ajax/file_upload_handler[^\s\'"]*', res.text)
        if match:
            _RESOLVED_UPLOAD_URL = match.group(0).replace("&amp;", "&")
            print(f"[+] Auto-discovered Storage Node: {_RESOLVED_UPLOAD_URL}", flush=True)
            return _RESOLVED_UPLOAD_URL
    except Exception as exc:
        print(f"[-] Node resolution warning: {exc}", flush=True)

    raise RuntimeError("Could not resolve Megaup Storage Node. Please check if cookies are valid.")

def sanitize_name(name: str) -> str:
    normalized = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('ASCII')
    clean = re.sub(r'[\/:*?"<>|]', ' - ', normalized)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean if clean else "file_" + str(int(time.time()))

def create_or_get_folder(folder_name: str, parent_id: str = None) -> str:
    parent_id = str(parent_id or ROOT_FOLDER_ID)
    clean_folder = sanitize_name(folder_name)[:80]
    cache_key = f"{parent_id}/{clean_folder}"
    if cache_key in folder_cache:
        return folder_cache[cache_key]

    session = get_authenticated_session()
    url = f"{MEGAUP_BASE}/account/ajax/add_edit_folder_process"
    payload = {
        "folderName": clean_folder,
        "parentId": parent_id,
        "parent_folder_id": parent_id,
        "isPublic": "1",
        "password": "",
        "watermarkPreviews": "0",
        "showDownloadLinks": "1",
        "submitme": "1",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{MEGAUP_BASE}/",
        "Origin": MEGAUP_BASE,
    }
    try:
        res = session.post(url, data=payload, headers=headers, timeout=30)
        data = res.json()
        if data.get("success") and data.get("folder_id"):
            new_id = str(data["folder_id"])
            folder_cache[cache_key] = new_id
            print(f"  [+] Created Folder '{clean_folder}' -> ID: {new_id}", flush=True)
            return new_id
    except Exception as exc:
        print(f"  [-] Folder create warning for '{clean_folder}': {exc}", flush=True)

    return parent_id

def move_file_to_folder(file_id: str, target_folder_id: str) -> bool:
    session = get_authenticated_session()
    endpoints = [
        f"{MEGAUP_BASE}/account/ajax/drag_files_into_folder",
        f"{MEGAUP_BASE}/ajax/drag_files_into_folder",
    ]
    payloads = [
        {"fileIds[]": str(file_id), "folderId": str(target_folder_id)},
        {"fileIds": str(file_id), "folderId": str(target_folder_id)},
    ]
    for ep in endpoints:
        for p in payloads:
            try:
                res = session.post(ep, data=p, timeout=20)
                if res.status_code == 200:
                    return True
            except Exception:
                pass
    return False

def megaup_upload_file(file_path: str, target_folder_id: str) -> bool:
    target_id = str(target_folder_id or ROOT_FOLDER_ID)
    path = pathlib.Path(file_path)
    total_size = path.stat().st_size
    file_name = sanitize_name(path.name)
    c_tracker = str(uuid.uuid4())

    session = get_authenticated_session()
    upload_url = resolve_upload_url(session)

    bytes_sent = 0
    res_data = None
    last_reported = -10

    with open(path, "rb") as fh:
        while bytes_sent < total_size:
            chunk_data = fh.read(CHUNK_SIZE)
            chunk_len = len(chunk_data)
            if not chunk_data:
                break

            range_start = bytes_sent
            range_end = bytes_sent + chunk_len - 1

            form_data = {
                "folder_id": target_id,
                "folderId": target_id,
                "upload_folder": target_id,
                "upload_folder_id": target_id,
                "c_tracker": c_tracker,
                "max_chunk_size": str(CHUNK_SIZE),
            }

            headers = {
                "Content-Range": f"bytes {range_start}-{range_end}/{total_size}",
                "X-Requested-With": "XMLHttpRequest",
            }

            files = {
                "files[]": (file_name, chunk_data, "application/octet-stream")
            }

            chunk_ok = False
            for attempt in range(1, 4):
                try:
                    response = session.post(upload_url, data=form_data, files=files, headers=headers, timeout=180)
                    response.raise_for_status()
                    try:
                        res_data = response.json()
                    except Exception:
                        pass
                    chunk_ok = True
                    break
                except Exception as e:
                    print(f"    [!] Chunk attempt {attempt} failed: {e}. Retrying in 5s...", flush=True)
                    time.sleep(5)

            if not chunk_ok:
                print(f"    [-] Chunk upload failed for {file_name}", flush=True)
                return False

            bytes_sent += chunk_len
            pct = int((bytes_sent / total_size) * 100)
            if pct >= last_reported + 10 or pct == 100:
                print(f"    -> Progress: {pct}% ({bytes_sent/(1024*1024):.1f}/{total_size/(1024*1024):.1f} MB)", flush=True)
                last_reported = pct

    if isinstance(res_data, list) and len(res_data) > 0:
        res_data = res_data[0]

    if isinstance(res_data, dict):
        if res_data.get("error"):
            print(f"    [-] Megaup Server Error: {res_data.get('error')}", flush=True)
            return False
        file_id = res_data.get("file_id")
        if file_id and target_id != str(ROOT_FOLDER_ID):
            move_file_to_folder(file_id, target_id)

    return True

# --- Main Flow ---
print("[*] Initializing MegaUp Session...", flush=True)
s = get_authenticated_session()
node_url = resolve_upload_url(s)

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
    raw_filename = os.path.basename(rel_path)
    file_size_gb = file_size / (1024 ** 3)

    sub_dir = os.path.dirname(rel_path)
    target_folder_id = ROOT_FOLDER_ID
    if sub_dir:
        curr_parent = ROOT_FOLDER_ID
        for part in sub_dir.split("/"):
            if part:
                curr_parent = create_or_get_folder(part, curr_parent)
        target_folder_id = curr_parent

    safe_local_name = sanitize_name(raw_filename)
    local_path = os.path.join(TEMP_DIR, safe_local_name)
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
            print(f"    -> Uploading chunk part: {part_name}...", flush=True)
            ok = megaup_upload_file(part, target_folder_id)
            if ok:
                print(f"    [+] Chunk uploaded successfully!", flush=True)
                os.remove(part)
            else:
                upload_success_all_parts = False
                break
    else:
        print(f"  -> Uploading to MegaUp Folder ID: {target_folder_id}...", flush=True)
        ok = megaup_upload_file(local_path, target_folder_id)
        if ok:
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
