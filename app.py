"""
VidGrab Backend — Flask + yt-dlp
=================================
Requirements:
    pip install flask flask-cors yt-dlp

Run:
    python app.py
"""

import os
import re
import shlex
import subprocess
import threading
import json
import time
from pathlib import Path
from flask import Flask, request, Response, send_from_directory, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("download")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Auto-clean files older than 1 hour
def clean_old_files():
    while True:
        time.sleep(600)
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            try:
                if f.is_file() and (now - f.stat().st_mtime) > 3600:
                    f.unlink()
                    print(f"[clean] Deleted old file: {f.name}")
            except Exception as e:
                print(f"[clean] Error: {e}")

threading.Thread(target=clean_old_files, daemon=True).start()


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "VidGrab backend is running", "version": "1.0"})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    if not data or "command" not in data:
        return jsonify({"error": "command required"}), 400

    raw_command = data["command"].strip()

    # Security: block dangerous shell characters
    BLOCKED = [";", "&&", "||", "`", "$(", "\n", "\r"]
    for b in BLOCKED:
        if b in raw_command:
            return jsonify({"error": f"Blocked character: {b}"}), 400

    def generate():
        try:
            args = shlex.split(raw_command)
            cmd = [
                "yt-dlp",
                "--newline",
                "--no-playlist-reverse",
                "--no-color",
                "--no-check-certificates",
                "-o", "download/%(title)s.%(ext)s",
                "--restrict-filenames",
                "--no-part",
            ] + args

            # Filter out duplicate -o flags
            filtered = []
            skip_next = False
            for i, a in enumerate(cmd):
                if skip_next:
                    skip_next = False
                    continue
                if a == "-o" and i > 5:
                    skip_next = True
                    continue
                filtered.append(a)

            process = subprocess.Popen(
                filtered,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            last_filename = None
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                yield json.dumps({"type": "log", "text": line}) + "\n"

                m = re.search(r'Destination:\s+download/(.+)', line)
                if m:
                    last_filename = m.group(1)
                m2 = re.search(r'Merging formats into "download/(.+)"', line)
                if m2:
                    last_filename = m2.group(1)

            process.wait()

            if process.returncode == 0:
                files = sorted(DOWNLOAD_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
                if files:
                    last_filename = files[0].name

                yield json.dumps({
                    "type": "done",
                    "filename": last_filename or "video.mp4",
                    "returncode": 0
                }) + "\n"
            else:
                yield json.dumps({
                    "type": "error",
                    "text": f"yt-dlp error code: {process.returncode}"
                }) + "\n"

        except FileNotFoundError:
            yield json.dumps({
                "type": "error",
                "text": "yt-dlp installed nai! 'pip install yt-dlp' chalান।"
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "text": str(e)}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/file/<filename>", methods=["GET"])
def serve_file(filename):
    safe_name = Path(filename).name
    filepath = DOWNLOAD_DIR / safe_name
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(
        DOWNLOAD_DIR.resolve(),
        safe_name,
        as_attachment=True
    )


@app.route("/list", methods=["GET"])
def list_files():
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "age_seconds": int(time.time() - f.stat().st_mtime)
            })
    return jsonify({"files": files})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✓ VidGrab backend চালু হচ্ছে → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
