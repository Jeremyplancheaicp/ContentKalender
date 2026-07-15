#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Content Kalender — AI Studio
by Jeremy Planche
"""

from flask import Flask, request, jsonify, send_from_directory, send_file
import json, uuid, os, threading, webbrowser
from pathlib import Path
from datetime import datetime

BASE      = Path(__file__).parent
DATA_DIR  = Path(os.environ.get("DATA_DIR", BASE))
UPLOADS   = DATA_DIR / "uploads"
DATA      = DATA_DIR / "data.json"
KALENDER  = DATA_DIR / "kalender.json"
UPLOADS.mkdir(parents=True, exist_ok=True)

# Seed persistent volume from the bundled repo data on first boot.
if DATA_DIR != BASE:
    import shutil
    if not DATA.exists() and (BASE / "data.json").exists():
        shutil.copy(BASE / "data.json", DATA)
    if not KALENDER.exists() and (BASE / "kalender.json").exists():
        shutil.copy(BASE / "kalender.json", KALENDER)
    if not any(UPLOADS.iterdir()) and (BASE / "uploads").is_dir():
        for f in (BASE / "uploads").iterdir():
            shutil.copy(f, UPLOADS / f.name)

app = Flask(__name__, static_folder=str(BASE))

PLATFORMS = ["Instagram", "TikTok", "Fanvue", "Facebook", "Threads"]

def load():
    if DATA.exists():
        return json.loads(DATA.read_text())
    return {"personas": ["Luna", "Mia", "Zara"], "posts": []}

def save(d):
    DATA.write_text(json.dumps(d, ensure_ascii=False, indent=2))


@app.route("/")
def index():
    return send_from_directory(str(BASE), "index.html")

@app.route("/data", methods=["GET"])
def get_data():
    return jsonify(load())

@app.route("/personas", methods=["POST"])
def add_persona():
    d = load()
    name = request.json.get("name", "").strip()
    if name and name not in d["personas"]:
        d["personas"].append(name)
        save(d)
    return jsonify(d["personas"])

@app.route("/personas/<name>", methods=["DELETE"])
def del_persona(name):
    d = load()
    d["personas"] = [p for p in d["personas"] if p != name]
    save(d)
    return jsonify(d["personas"])

@app.route("/posts", methods=["POST"])
def add_post():
    d = load()
    post = {
        "id":       str(uuid.uuid4()),
        "persona":  request.form.get("persona", ""),
        "platform": request.form.get("platform", "Instagram"),
        "date":     request.form.get("date", ""),
        "time":     request.form.get("time", "12:00"),
        "caption":  request.form.get("caption", ""),
        "hashtags": request.form.get("hashtags", ""),
        "media":    "",
        "status":   "geplant",
    }
    f = request.files.get("media")
    if f and f.filename:
        ext  = Path(f.filename).suffix
        name = str(uuid.uuid4()) + ext
        f.save(str(UPLOADS / name))
        post["media"] = name
    d["posts"].append(post)
    save(d)
    return jsonify(post)

@app.route("/posts/<pid>", methods=["PUT"])
def update_post(pid):
    d = load()
    for p in d["posts"]:
        if p["id"] == pid:
            if request.is_json:
                p.update(request.json)
            else:
                for k in ["persona","platform","date","time","caption","hashtags","status"]:
                    if k in request.form:
                        p[k] = request.form[k]
                f = request.files.get("media")
                if f and f.filename:
                    ext  = Path(f.filename).suffix
                    name = str(uuid.uuid4()) + ext
                    f.save(str(UPLOADS / name))
                    p["media"] = name
            save(d)
            return jsonify(p)
    return jsonify({"error": "nicht gefunden"}), 404

@app.route("/posts/<pid>", methods=["DELETE"])
def del_post(pid):
    d = load()
    d["posts"] = [p for p in d["posts"] if p["id"] != pid]
    save(d)
    return jsonify({"ok": True})

@app.route("/kalender", methods=["GET"])
def get_kalender():
    if KALENDER.exists():
        return jsonify(json.loads(KALENDER.read_text()))
    return jsonify(None)

BACKUPS = DATA_DIR / "backups"

@app.route("/kalender", methods=["POST"])
def save_kalender():
    if KALENDER.exists():
        BACKUPS.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUPS / f"kalender-{stamp}.json"
        backup_path.write_text(KALENDER.read_text())
        # Nur die letzten 50 Backups behalten
        old = sorted(BACKUPS.glob("kalender-*.json"))
        for f in old[:-50]:
            f.unlink()
    KALENDER.write_text(json.dumps(request.json, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})

@app.route("/debug/backups-list")
def debug_backups_list():
    if not BACKUPS.exists():
        return jsonify([])
    return jsonify(sorted(f.name for f in BACKUPS.glob("kalender-*.json")))

@app.route("/debug/uploads-list")
def debug_uploads_list():
    return jsonify(sorted(f.name for f in UPLOADS.iterdir() if f.is_file()))

@app.route("/debug/orphans")
def debug_orphans():
    referenced = set()
    if KALENDER.exists():
        cal = json.loads(KALENDER.read_text())
        for persona in cal.get("byPersona", {}).values():
            for day in persona.get("days", []):
                for key in ("bilder", "story"):
                    slot = day.get(key)
                    if not slot:
                        continue
                    for f in slot.get("files", []):
                        u = f.get("url", "")
                        if u.startswith("/uploads/"):
                            referenced.add(u.split("/")[-1])
    orphans = sorted(f.name for f in UPLOADS.iterdir() if f.is_file() and f.name not in referenced)
    items = "".join(
        f'<div style="display:inline-block;margin:8px;text-align:center;vertical-align:top">'
        f'<img src="/uploads/{o}" style="max-width:180px;max-height:180px;border-radius:8px;display:block;object-fit:cover">'
        f'<div style="font-size:11px;color:#888;word-break:break-all;max-width:180px;margin-top:4px">{o}</div>'
        f'</div>'
        for o in orphans
    )
    return (f"<html><body style='background:#111;color:#eee;font-family:sans-serif;padding:20px'>"
            f"<h2>{len(orphans)} nicht zugeordnete Bilder</h2>"
            f"<p>Diese Dateien liegen noch auf dem Server, sind aber in keinem Kalendertag mehr verlinkt.</p>"
            f"{items}</body></html>")

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Keine Datei"}), 400
    ext  = Path(f.filename).suffix or ".png"
    name = str(uuid.uuid4()) + ext
    f.save(str(UPLOADS / name))
    return jsonify({"url": f"/uploads/{name}"})

@app.route("/uploads/<filename>")
def media(filename):
    return send_from_directory(str(UPLOADS), filename)


if __name__ == "__main__":
    import socket
    port = int(os.environ.get("PORT", 5056))
    local_ip = socket.gethostbyname(socket.gethostname())
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print(f"✓ Content Kalender läuft auf:")
    print(f"  Dieser Mac:    http://localhost:{port}")
    print(f"  Andere Geräte: http://{local_ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
