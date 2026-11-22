import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
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


def tg_call(method: str, payload: dict | None = None) -> dict:
    url = f"{TG_API_BASE}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_reply(text: str):
    try:
        tg_call("sendMessage", {"chat_id": ALLOWED_CHAT_ID, "text": text})
    except Exception as exc:
        print("send_reply error:", exc, flush=True)


def parse_message(text: str):
    parts = text.strip().split()
    if parts and parts[0].startswith("/clip"):
        parts = parts[1:]
    if not parts or not parts[0].startswith("http"):
        return None
    url = parts[0]
    section = parts[1] if len(parts) > 1 else "0:00:00-1:00:00"
    if section.lower() == "full":
        section = ""
    clips = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 3
    posts = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return url, section, clips, posts


def push_and_monitor_kernel(stream_url: str, section: str, clips: int, posts: int):
    work_dir = Path(tempfile.mkdtemp(prefix="tg_kernel_"))
    try:
        send_reply(f"📥 Pulling {KERNEL_SLUG} and updating settings...")
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

        for cell in nb.get("cells", []):
            if cell.get("cell_type") == "code":
                src = "".join(cell.get("source", []))
                if "STREAM_URL" in src:
                    src = re.sub(r'STREAM_URL\s*=\s*".*?"', f'STREAM_URL = "{stream_url}"', src)
                    src = re.sub(r'SECTION\s*=\s*".*?"', f'SECTION = "{section}"', src)
                    src = re.sub(r"CLIPS_COUNT\s*=\s*\d+", f"CLIPS_COUNT = {clips}", src)
                    src = re.sub(r"POST_COUNT\s*=\s*\d+", f"POST_COUNT = {posts}", src)
                    cell["source"] = [line + "\n" for line in src.splitlines()]
                    break

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
            f"🚀 Kaggle run started!\n"
            f"• URL: {stream_url}\n"
            f"• Section: {section or 'full'}\n"
            f"• Clips: {clips} | Reels to post: {posts}"
        )

        while True:
            time.sleep(60)
            status = subprocess.check_output(
                ["kaggle", "kernels", "status", KERNEL_SLUG],
                text=True,
            ).strip()
            low = status.lower()
            if "complete" in low:
                send_reply(f"✅ Kaggle run completed!\n{status}")
                break
            if "error" in low or "cancel" in low:
                send_reply(f"⚠️ Kaggle run ended:\n{status}")
                break

    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc))[:800]
        send_reply(f"❌ Kaggle CLI error:\n{err}")
    except Exception as exc:
        send_reply(f"❌ Error:\n{str(exc)[:800]}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    send_reply("🤖 Bot online. Send a stream URL or `/clip <url> <section|full> <clips> <posts>`.")

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
                parsed = parse_message(text)
                if not parsed:
                    continue
                threading.Thread(target=push_and_monitor_kernel, args=parsed, daemon=True).start()
        except Exception as exc:
            print("Telegram poll error:", exc, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
