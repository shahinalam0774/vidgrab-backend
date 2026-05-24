"""
VidGrab Backend v2.0 — Flask + yt-dlp
======================================
pip install flask flask-cors yt-dlp gunicorn
python app.py
"""

import os, re, shlex, subprocess, threading, json, time
from pathlib import Path
from flask import Flask, request, Response, send_from_directory, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("download")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── Auto-clean files older than 1 hour ──
def clean_old_files():
    while True:
        time.sleep(600)
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            try:
                if f.is_file() and (now - f.stat().st_mtime) > 3600:
                    f.unlink()
                    print(f"[clean] Deleted: {f.name}")
            except Exception as e:
                print(f"[clean] Error: {e}")

threading.Thread(target=clean_old_files, daemon=True).start()


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "VidGrab v2.0 running", "version": "2.0"})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    if not data or "command" not in data:
        return jsonify({"error": "command required"}), 400

    raw = data["command"].strip()

    BLOCKED = [";", "&&", "||", "`", "$(", "\n", "\r"]
    for b in BLOCKED:
        if b in raw:
            return jsonify({"error": f"Blocked: {b}"}), 400

    def generate():
        try:
            args = shlex.split(raw)

            cmd = [
                "yt-dlp",
                "--newline",
                "--no-playlist-reverse",
                "--no-color",
                "--no-check-certificates",
                "--restrict-filenames",
                "--no-part",
                "-o", "download/%(playlist_index)s-%(title)s.%(ext)s"
                       if any(a in raw for a in ["--playlist", "playlist_items", "playlist-start"])
                       else "download/%(title)s.%(ext)s",
            ] + args

            # Remove duplicate -o
            filtered, skip = [], False
            for i, a in enumerate(cmd):
                if skip: skip = False; continue
                if a == "-o" and i > 8: skip = True; continue
                filtered.append(a)

            # Track files seen before this run
            before = set(f.name for f in DOWNLOAD_DIR.iterdir() if f.is_file())
            seen_ready = set()

            process = subprocess.Popen(
                filtered,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )

            last_filename = None
            last_size = ""
            merging = False

            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue

                yield json.dumps({"type": "log", "text": line}) + "\n"

                # Track destination
                m = re.search(r'Destination:\s+download/(.+)', line)
                if m:
                    last_filename = m.group(1)
                    merging = False

                m2 = re.search(r'Merging formats into "download/(.+)"', line)
                if m2:
                    last_filename = m2.group(1)
                    merging = True

                # Track size
                sm = re.search(r'of\s+([\d.]+\s*\w+)\s+in', line)
                if sm:
                    last_size = sm[0].replace("of ", "").split(" in")[0].strip()

                # Detect "Deleting original" = merge done, file ready
                if "Deleting original file" in line and merging and last_filename:
                    fp = DOWNLOAD_DIR / last_filename
                    if fp.exists() and last_filename not in seen_ready:
                        seen_ready.add(last_filename)
                        fsize = _fmt_size(fp.stat().st_size)
                        yield json.dumps({
                            "type": "file_ready",
                            "filename": last_filename,
                            "filesize": fsize,
                        }) + "\n"

                # Detect 100% download for non-merged (audio only) files
                if "[download] 100%" in line and last_filename and not merging:
                    fp = DOWNLOAD_DIR / last_filename
                    if fp.exists() and last_filename not in seen_ready:
                        seen_ready.add(last_filename)
                        fsize = _fmt_size(fp.stat().st_size)
                        yield json.dumps({
                            "type": "file_ready",
                            "filename": last_filename,
                            "filesize": fsize,
                        }) + "\n"

            process.wait()

            # Any new files not yet announced
            after = set(f.name for f in DOWNLOAD_DIR.iterdir() if f.is_file())
            new_files = after - before - seen_ready
            for fname in new_files:
                fp = DOWNLOAD_DIR / fname
                fsize = _fmt_size(fp.stat().st_size)
                yield json.dumps({
                    "type": "file_ready",
                    "filename": fname,
                    "filesize": fsize,
                }) + "\n"

            if process.returncode == 0:
                # pick most recent file for "done" event
                all_files = sorted(DOWNLOAD_DIR.iterdir(),
                                   key=lambda f: f.stat().st_mtime, reverse=True)
                final = all_files[0].name if all_files else (last_filename or "video.mp4")
                yield json.dumps({"type": "done", "filename": final, "returncode": 0}) + "\n"
            else:
                yield json.dumps({"type": "error", "text": f"yt-dlp exit code: {process.returncode}"}) + "\n"

        except FileNotFoundError:
            yield json.dumps({"type": "error", "text": "yt-dlp not found. Run: pip install yt-dlp"}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "text": str(e)}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


def _fmt_size(n):
    if n < 1024: return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.2f} GB"


@app.route("/file/<filename>", methods=["GET"])
def serve_file(filename):
    safe = Path(filename).name
    fp = DOWNLOAD_DIR / safe
    if not fp.exists():
        return jsonify({"error": "File not found or expired"}), 404
    return send_from_directory(DOWNLOAD_DIR.resolve(), safe, as_attachment=True)


@app.route("/list", methods=["GET"])
def list_files():
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": _fmt_size(f.stat().st_size),
                "age_seconds": int(time.time() - f.stat().st_mtime),
            })
    return jsonify({"files": files})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✓ VidGrab v2.0 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
