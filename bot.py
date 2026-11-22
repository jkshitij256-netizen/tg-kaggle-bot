import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

BOT_TOKEN = os.environ["TG_BOT_TOKEN"].strip()
ALLOWED_CHAT_ID = int(os.environ["TG_CHAT_ID"].strip())
KERNEL_SLUG = os.environ["KERNEL_SLUG"].strip()

# Support KGAT_... bearer token if placed in KAGGLE_API_TOKEN
kg_api_token = os.environ.get("KAGGLE_API_TOKEN", "").strip()
if kg_api_token:
    kg_dir = Path.home() / ".kaggle"
    kg_dir.mkdir(parents=True, exist_ok=True)
    token_file = kg_dir / "access_token"
    token_file.write_text(kg_api_token + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

TG_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_TG_VIDEO_BYTES = 50 * 1024 * 1024  # 50 MB Bot API upload limit
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm")

# Per-chat interactive wizard state: chat_id -> {"step": ..., "config": {...}}
PENDING_SESSIONS: dict[int, dict] = {}
# Per-chat active run cancel event: chat_id -> threading.Event
ACTIVE_RUN_CANCELS: dict[int, threading.Event] = {}
STATE_LOCK = threading.Lock()


def tg_call(method: str, payload: dict | None = None) -> dict:
    url = f"{TG_API_BASE}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_upload_video(chat_id: int, video_path: Path, caption: str = "") -> dict:
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    file_bytes = video_path.read_bytes()

    parts: list[bytes] = []

    def add_field(name: str, value: str):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", caption[:1024])
    add_field("supports_streaming", "true")

    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="video"; filename="{video_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    url = f"{TG_API_BASE}/sendVideo"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_reply(text: str, chat_id: int | None = None):
    target_id = chat_id if chat_id is not None else ALLOWED_CHAT_ID
    try:
        tg_call("sendMessage", {"chat_id": target_id, "text": text})
    except Exception as exc:
        print("send_reply error:", exc, flush=True)


def get_kernel_status() -> str:
    return subprocess.check_output(
        ["kaggle", "kernels", "status", KERNEL_SLUG],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def check_and_send_status(chat_id: int):
    try:
        status = get_kernel_status()
        send_reply(f"📊 Kaggle status for `{KERNEL_SLUG}`:\n{status}", chat_id=chat_id)
    except subprocess.CalledProcessError as exc:
        err = (exc.output or str(exc))[:800]
        send_reply(f"❌ Status check error:\n{err}", chat_id=chat_id)
    except Exception as exc:
        send_reply(f"❌ Status check error:\n{str(exc)[:800]}", chat_id=chat_id)


def handle_cancel_request(chat_id: int):
    cancelled_wizard = False
    cancelled_run = False

    with STATE_LOCK:
        if chat_id in PENDING_SESSIONS:
            PENDING_SESSIONS.pop(chat_id, None)
            cancelled_wizard = True
        run_ev = ACTIVE_RUN_CANCELS.get(chat_id)
        if run_ev is not None:
            run_ev.set()
            cancelled_run = True

    if cancelled_wizard and not cancelled_run:
        send_reply("🛑 Setup cancelled. Send a stream URL anytime to start again.", chat_id=chat_id)
    elif cancelled_run:
        send_reply(
            "🛑 Monitoring stopped for the active run.\n"
            "Note: Kaggle CLI does not stop a remote session by slug alone—if the notebook is still running on Kaggle, "
            "stop its session in Kaggle's Active Events / Sessions tab so GPU/quota isn't used.",
            chat_id=chat_id,
        )
    else:
        send_reply("ℹ️ No active setup prompt or monitored run to cancel right now.", chat_id=chat_id)


def extract_url_if_present(text: str) -> str | None:
    parts = text.strip().split()
    if parts and parts[0].startswith("/clip"):
        parts = parts[1:]
    if parts and parts[0].startswith("http"):
        return parts[0]
    return None


def set_py_assignment(src: str, var_name: str, value: str, is_string: bool = True) -> str:
    """Replace an existing assignment or append it to Cell 1 if missing."""
    if is_string:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        replacement = f'{var_name} = "{escaped}"'
    else:
        replacement = f"{var_name} = {value}"
    pattern = rf"(?m)^[ \t]*{re.escape(var_name)}\s*=.*$"

    if re.search(pattern, src):
        return re.sub(pattern, replacement, src)
    return src.rstrip() + f"\n{replacement}\n"


def update_cell_1_source(src: str, cfg: dict) -> str:
    should_post = bool(cfg["should_post"])
    post_count = int(cfg["post_count"]) if should_post else 0
    captions_enabled = bool(cfg["captions_enabled"])
    captions_style = str(cfg["captions_style"])

    src = set_py_assignment(src, "STREAM_URL", cfg["stream_url"], is_string=True)
    src = set_py_assignment(src, "CLIPS_COUNT", str(int(cfg["clips_count"])), is_string=False)
    src = set_py_assignment(src, "CLIP_LENGTH", cfg["clip_length"], is_string=True)
    src = set_py_assignment(src, "SECTION", cfg["clip_length"], is_string=True)
    src = set_py_assignment(src, "SHOULD_POST", "True" if should_post else "False", is_string=False)
    src = set_py_assignment(src, "AUTO_POST", "True" if should_post else "False", is_string=False)
    src = set_py_assignment(src, "POST_COUNT", str(post_count), is_string=False)
    src = set_py_assignment(src, "CAPTIONS", "True" if captions_enabled else "False", is_string=False)
    src = set_py_assignment(src, "ENABLE_CAPTIONS", "True" if captions_enabled else "False", is_string=False)
    src = set_py_assignment(src, "CAPTION_STYLE", captions_style, is_string=True)
    return src


def extract_zip_archives(root_dir: Path):
    """Extract any .zip files in root_dir into subfolders safely."""
    zip_files = [p for p in root_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".zip"]
    for zp in zip_files:
        extract_dir = zp.parent / f"_unzipped_{zp.stem}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                for member in zf.infolist():
                    target_path = (extract_dir / member.filename).resolve()
                    if not str(target_path).startswith(str(extract_dir.resolve())):
                        continue
                    zf.extract(member, extract_dir)
        except zipfile.BadZipFile:
            print("Skipping invalid zip archive:", zp.name, flush=True)


def fetch_and_send_clips(chat_id: int, cancel_event: threading.Event):
    out_dir = Path(tempfile.mkdtemp(prefix="tg_clips_"))
    try:
        if cancel_event.is_set():
            return
        send_reply(f"⬇️ Downloading generated output from `{KERNEL_SLUG}`...", chat_id=chat_id)
        subprocess.run(
            ["kaggle", "kernels", "output", KERNEL_SLUG, "-p", str(out_dir), "--force"],
            check=True,
            capture_output=True,
            text=True,
        )

        if cancel_event.is_set():
            return

        extract_zip_archives(out_dir)

        video_files = sorted(
            [p for p in out_dir.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
            key=lambda p: p.name.lower(),
        )

        if not video_files:
            found_names = [p.name for p in out_dir.rglob("*") if p.is_file()]
            send_reply(
                f"ℹ️ Run completed, but no video files were found.\nFiles downloaded: {', '.join(found_names) or 'none'}",
                chat_id=chat_id,
            )
            return

        sent_count = 0
        for idx, vid_path in enumerate(video_files, start=1):
            if cancel_event.is_set():
                send_reply("🛑 Clip upload stopped because /cancel was sent.", chat_id=chat_id)
                return

            size_mb = vid_path.stat().st_size / (1024 * 1024)
            if vid_path.stat().st_size > MAX_TG_VIDEO_BYTES:
                send_reply(
                    f"⚠️ Skipped `{vid_path.name}` ({size_mb:.1f} MB) — exceeds Telegram's 50 MB bot upload limit.",
                    chat_id=chat_id,
                )
                continue

            send_reply(f"📤 Sending clip {idx}/{len(video_files)}: `{vid_path.name}` ({size_mb:.1f} MB)...", chat_id=chat_id)
            tg_upload_video(chat_id=chat_id, video_path=vid_path, caption=f"🎬 {vid_path.name}")
            sent_count += 1

        send_reply(f"✅ Sent {sent_count} clip(s) to Telegram.", chat_id=chat_id)

    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc))[:800]
        send_reply(f"❌ Output download error:\n{err}", chat_id=chat_id)
    except Exception as exc:
        send_reply(f"❌ Clip delivery error:\n{str(exc)[:800]}", chat_id=chat_id)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def push_and_monitor_kernel(cfg: dict, chat_id: int, cancel_event: threading.Event):
    work_dir = Path(tempfile.mkdtemp(prefix="tg_kernel_"))
    try:
        send_reply(f"📥 Pulling {KERNEL_SLUG} and updating Cell 1...", chat_id=chat_id)
        subprocess.run(
            ["kaggle", "kernels", "pull", "-m", "-p", str(work_dir), "-k", KERNEL_SLUG],
            check=True,
            capture_output=True,
            text=True,
        )

        ipynb_files = list(work_dir.glob("*.ipynb"))
        if not ipynb_files:
            raise FileNotFoundError("No .ipynb file returned by kaggle kernels pull.")
        nb_path = ipynb_files[0]
        nb = json.loads(nb_path.read_text(encoding="utf-8"))

        cells = nb.get("cells", [])
        target_idx = None
        for idx, cell in enumerate(cells):
            if cell.get("cell_type") == "code":
                src = "".join(cell.get("source", []))
                if "STREAM_URL" in src or idx == 0:
                    target_idx = idx
                    if "STREAM_URL" in src:
                        break

        if target_idx is not None:
            old_src = "".join(cells[target_idx].get("source", []))
            new_src = update_cell_1_source(old_src, cfg)
            cells[target_idx]["source"] = [line + "\n" for line in new_src.splitlines()]

        nb_path.write_text(json.dumps(nb, indent=2), encoding="utf-8")

        meta_path = work_dir / "kernel-metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["enable_internet"] = True
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        subprocess.run(
            ["kaggle", "kernels", "push", "-p", str(work_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        send_reply(
            f"🚀 Kaggle run pushed!\n"
            f"• URL: {cfg['stream_url']}\n"
            f"• Clips: {cfg['clips_count']}\n"
            f"• Length/Section: {cfg['clip_length'] or 'full'}\n"
            f"• Post: {'Yes (' + str(cfg['post_count']) + ')' if cfg['should_post'] else 'No'}\n"
            f"• Captions: {'Yes (' + cfg['captions_style'] + ')' if cfg['captions_enabled'] else 'No'}\n"
            f"Send /status to check progress or /cancel to stop monitoring.",
            chat_id=chat_id,
        )

        last_status = None
        while not cancel_event.is_set():
            if cancel_event.wait(timeout=60):
                break
            status = get_kernel_status()
            if status != last_status:
                send_reply(f"🔄 Status update:\n{status}", chat_id=chat_id)
                last_status = status

            low = status.lower()
            if "complete" in low:
                send_reply(f"✅ Kaggle run completed!\n{status}", chat_id=chat_id)
                fetch_and_send_clips(chat_id, cancel_event)
                break
            if "error" in low or "cancel" in low:
                send_reply(f"⚠️ Kaggle run ended:\n{status}", chat_id=chat_id)
                break

    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc))[:800]
        send_reply(f"❌ Kaggle CLI error:\n{err}", chat_id=chat_id)
    except Exception as exc:
        send_reply(f"❌ Error:\n{str(exc)[:800]}", chat_id=chat_id)
    finally:
        with STATE_LOCK:
            if ACTIVE_RUN_CANCELS.get(chat_id) is cancel_event:
                ACTIVE_RUN_CANCELS.pop(chat_id, None)
        shutil.rmtree(work_dir, ignore_errors=True)


def ask_next_question(chat_id: int, step: str):
    if step == "clips_count":
        send_reply(
            "1️⃣ **How many clips** do you want to generate? (e.g. `3`, or type `skip` for `3`)",
            chat_id=chat_id,
        )
    elif step == "clip_length":
        send_reply(
            "2️⃣ **What clip length or section**? (e.g. `60s`, `0:00:00-0:30:00`, or `full` / `skip` for `0:00:00-1:00:00`)",
            chat_id=chat_id,
        )
    elif step == "should_post":
        send_reply(
            "3️⃣ **Is it going to post or not?** Reply `yes` or `no` (or `skip` for `yes`)",
            chat_id=chat_id,
        )
    elif step == "post_count":
        send_reply(
            "3b️⃣ **How many posts/reels** should it publish? (e.g. `1`, or `skip` for `1`)",
            chat_id=chat_id,
        )
    elif step == "captions":
        send_reply(
            "4️⃣ **Captions?** Reply `yes` / `no` or a caption style (e.g. `yes bold`, `no`, or `skip` for `yes`)",
            chat_id=chat_id,
        )


def start_run_thread(cfg: dict, chat_id: int):
    cancel_event = threading.Event()
    with STATE_LOCK:
        old_ev = ACTIVE_RUN_CANCELS.get(chat_id)
        if old_ev is not None:
            old_ev.set()
        ACTIVE_RUN_CANCELS[chat_id] = cancel_event

    threading.Thread(target=push_and_monitor_kernel, args=(cfg, chat_id, cancel_event), daemon=True).start()


def handle_wizard_answer(chat_id: int, text: str):
    low = text.strip().lower()
    if low in ("/cancel", "cancel"):
        handle_cancel_request(chat_id)
        return

    with STATE_LOCK:
        session = PENDING_SESSIONS.get(chat_id)
    if not session:
        return

    step = session["step"]
    cfg = session["config"]
    is_default = low in ("skip", "default", "")

    if step == "clips_count":
        if is_default:
            cfg["clips_count"] = 3
        elif text.strip().isdigit() and int(text.strip()) > 0:
            cfg["clips_count"] = int(text.strip())
        else:
            send_reply("Please enter a valid positive number for clips (e.g. `3`) or `skip`.", chat_id=chat_id)
            return
        session["step"] = "clip_length"
        ask_next_question(chat_id, "clip_length")

    elif step == "clip_length":
        if is_default:
            cfg["clip_length"] = "0:00:00-1:00:00"
        elif low == "full":
            cfg["clip_length"] = ""
        else:
            cfg["clip_length"] = text.strip()
        session["step"] = "should_post"
        ask_next_question(chat_id, "should_post")

    elif step == "should_post":
        if is_default or low in ("y", "yes", "true", "1", "post"):
            cfg["should_post"] = True
            session["step"] = "post_count"
            ask_next_question(chat_id, "post_count")
        elif low in ("n", "no", "false", "0", "dont post", "don't post", "no post"):
            cfg["should_post"] = False
            cfg["post_count"] = 0
            session["step"] = "captions"
            ask_next_question(chat_id, "captions")
        else:
            send_reply("Please reply `yes` or `no` (or `skip`).", chat_id=chat_id)

    elif step == "post_count":
        if is_default:
            cfg["post_count"] = 1
        elif text.strip().isdigit() and int(text.strip()) >= 0:
            cfg["post_count"] = int(text.strip())
        else:
            send_reply("Please enter a valid number for posts (e.g. `1`) or `skip`.", chat_id=chat_id)
            return
        session["step"] = "captions"
        ask_next_question(chat_id, "captions")

    elif step == "captions":
        if is_default or low in ("y", "yes", "true", "on", "1"):
            cfg["captions_enabled"] = True
            cfg["captions_style"] = "default"
        elif low in ("n", "no", "false", "off", "0"):
            cfg["captions_enabled"] = False
            cfg["captions_style"] = "none"
        else:
            cfg["captions_enabled"] = True
            cfg["captions_style"] = text.strip()

        with STATE_LOCK:
            PENDING_SESSIONS.pop(chat_id, None)

        start_run_thread(cfg, chat_id)


def main():
    send_reply(
        "🤖 Bot online! Send a stream URL to configure Cell 1 step-by-step, `/status` for status, or `/cancel` to stop.",
        chat_id=ALLOWED_CHAT_ID,
    )

    offset = 0
    while True:
        try:
            res = tg_call("getUpdates", {"timeout": 30, "offset": offset})
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                if chat_id is not None:
                    print("INCOMING CHAT ID:", chat_id, "ALLOWED:", ALLOWED_CHAT_ID, flush=True)
                if chat_id != ALLOWED_CHAT_ID:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                low = text.lower()
                if low in ("/status", "status"):
                    threading.Thread(target=check_and_send_status, args=(chat_id,), daemon=True).start()
                    continue

                if low in ("/cancel", "cancel"):
                    handle_cancel_request(chat_id)
                    continue

                url = extract_url_if_present(text)
                if url:
                    with STATE_LOCK:
                        PENDING_SESSIONS[chat_id] = {
                            "step": "clips_count",
                            "config": {
                                "stream_url": url,
                                "clips_count": 3,
                                "clip_length": "0:00:00-1:00:00",
                                "should_post": True,
                                "post_count": 1,
                                "captions_enabled": True,
                                "captions_style": "default",
                            },
                        }
                    send_reply(f"🎬 Got URL:\n{url}\nLet's set up Cell 1 (type `cancel` anytime to stop).", chat_id=chat_id)
                    ask_next_question(chat_id, "clips_count")
                    continue

                with STATE_LOCK:
                    in_session = chat_id in PENDING_SESSIONS
                if in_session:
                    handle_wizard_answer(chat_id, text)

        except Exception as exc:
            print("Telegram poll error:", exc, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
