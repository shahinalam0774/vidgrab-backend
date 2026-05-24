"""
VidGrab Backend v3.1 — Flask + yt-dlp
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
COOKIES_FILE = Path("cookies.txt")

# ── Auto-clean files older than 1 hour ──
def clean_old_files():
    while True:
        time.sleep(600)
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            try:
                if f.is_file() and (now - f.stat().st_mtime) > 3600:
                    f.unlink()
            except Exception:
                pass

threading.Thread(target=clean_old_files, daemon=True).start()


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "VidGrab v3.1 running", "version": "3.1",
                    "cookies_loaded": COOKIES_FILE.exists()})


# ── Cookie endpoints ──
@app.route("/upload-cookies", methods=["POST"])
def upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "No file sent"}), 400
    f = request.files["file"]
    content = f.read().decode("utf-8", errors="ignore")
    if not content.strip():
        return jsonify({"error": "Empty file"}), 400
    COOKIES_FILE.write_text(content)
    return jsonify({"success": True})


@app.route("/cookie-status", methods=["GET"])
def cookie_status():
    return jsonify({"active": COOKIES_FILE.exists()})


@app.route("/delete-cookies", methods=["DELETE"])
def delete_cookies():
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
    return jsonify({"success": True})


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

            # ── Cookie resolution ──
            cookie_args = []
            user_has_cookies = "--cookies" in raw or "--cookies-from-browser" in raw

            if not user_has_cookies:
                # 1) Try browser (local machines)
                for browser in ["chrome", "firefox", "edge", "chromium"]:
                    try:
                        probe = subprocess.run(
                            ["yt-dlp", "--cookies-from-browser", browser,
                             "--skip-download", "--quiet", "--no-warnings",
                             "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
                            capture_output=True, text=True, timeout=8
                        )
                        if probe.returncode == 0:
                            cookie_args = ["--cookies-from-browser", browser]
                            break
                    except Exception:
                        continue

                # 2) Uploaded cookies.txt
                if not cookie_args and COOKIES_FILE.exists():
                    cookie_args = ["--cookies", str(COOKIES_FILE)]

            # ── Output template ──
            output_tpl = (
                "download/%(playlist_index)s-%(title)s.%(ext)s"
                if any(a in raw for a in ["--playlist", "playlist_items", "playlist-start"])
                else "download/%(title)s.%(ext)s"
            )

            # ── Build command ──
            # ios client: no JS runtime needed, no signature challenge, works with cookies
            cmd = [
                "yt-dlp",
                "--newline",
                "--no-playlist-reverse",
                "--no-color",
                "--no-check-certificates",
                "--restrict-filenames",
                "--no-part",
                "--extractor-args", "youtube:player_client=ios,android",
                "-o", output_tpl,
            ] + cookie_args + args

            # ── Deduplicate flags ──
            seen_o = False
            seen_cookies = False
            seen_extractor = 0  # allow max 1 injected extractor-args
            filtered = []
            i = 0
            while i < len(cmd):
                a = cmd[i]

                if a == "-o":
                    if not seen_o:
                        seen_o = True
                        filtered += [a, cmd[i+1]]
                    i += 2
                    continue

                if a in ("--cookies-from-browser", "--cookies"):
                    if not seen_cookies:
                        seen_cookies = True
                        filtered += [a, cmd[i+1]]
                    i += 2
                    continue

                filtered.append(a)
                i += 1

            # ── Run ──
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

                m = re.search(r'Destination:\s+download/(.+)', line)
                if m:
                    last_filename = m.group(1); merging = False

                m2 = re.search(r'Merging formats into "download/(.+)"', line)
                if m2:
                    last_filename = m2.group(1); merging = True

                sm = re.search(r'of\s+([\d.]+\s*\w+)\s+in', line)
                if sm:
                    last_size = sm[0].replace("of ", "").split(" in")[0].strip()

                if "Deleting original file" in line and merging and last_filename:
                    fp = DOWNLOAD_DIR / last_filename
                    if fp.exists() and last_filename not in seen_ready:
                        seen_ready.add(last_filename)
                        yield json.dumps({"type": "file_ready", "filename": last_filename,
                                          "filesize": _fmt_size(fp.stat().st_size)}) + "\n"

                if "[download] 100%" in line and last_filename and not merging:
                    fp = DOWNLOAD_DIR / last_filename
                    if fp.exists() and last_filename not in seen_ready:
                        seen_ready.add(last_filename)
                        yield json.dumps({"type": "file_ready", "filename": last_filename,
                                          "filesize": _fmt_size(fp.stat().st_size)}) + "\n"

            process.wait()

            after = set(f.name for f in DOWNLOAD_DIR.iterdir() if f.is_file())
            for fname in after - before - seen_ready:
                fp = DOWNLOAD_DIR / fname
                yield json.dumps({"type": "file_ready", "filename": fname,
                                  "filesize": _fmt_size(fp.stat().st_size)}) + "\n"

            if process.returncode == 0:
                all_files = sorted(DOWNLOAD_DIR.iterdir(),
                                   key=lambda f: f.stat().st_mtime, reverse=True)
                final = all_files[0].name if all_files else (last_filename or "video.mp4")
                yield json.dumps({"type": "done", "filename": final, "returncode": 0}) + "\n"
            else:
                yield json.dumps({"type": "error",
                                  "text": f"yt-dlp exit code: {process.returncode}"}) + "\n"

        except FileNotFoundError:
            yield json.dumps({"type": "error",
                              "text": "yt-dlp not found. Run: pip install yt-dlp"}) + "\n"
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
            files.append({"name": f.name, "size": _fmt_size(f.stat().st_size),
                          "age_seconds": int(time.time() - f.stat().st_mtime)})
    return jsonify({"files": files})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"✓ VidGrab v3.1 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
