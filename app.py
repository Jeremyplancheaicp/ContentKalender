#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Content Kalender — AI Studio
by Jeremy Planche

Robustheits-Update 2026-07 (verschwundene Bilder / Multi-Upload):
- Pro-Persona-Speichern mit Revisionscheck: zwei Creator, die gleichzeitig
  arbeiten, überschreiben sich nicht mehr gegenseitig den ganzen Kalender.
- Wipe-Guard auf dem alten Vollspeicher-Endpoint: ein veralteter Browser-Tab
  kann den Kalender nicht mehr leeren.
- Atomare Schreibvorgänge + Lock: kein halb geschriebenes/korruptes JSON mehr.
- Upload-Härtung: klare Fehler statt stillem Verschlucken, Größenlimit.
- Cache-Header für /uploads: Bilder laden schneller und belasten den Server
  nicht bei jedem Seitenaufruf neu (Dateinamen sind UUIDs → safe to cache).
- Backups: letzte 50 Speicherstände + 1 Tages-Backup pro Tag (30 Tage).
"""

from flask import Flask, request, jsonify, send_from_directory
import json, uuid, os, re, threading, webbrowser
from pathlib import Path
from datetime import datetime

BASE      = Path(__file__).parent
DATA_DIR  = Path(os.environ.get("DATA_DIR", BASE))
UPLOADS   = DATA_DIR / "uploads"
DATA      = DATA_DIR / "data.json"
KALENDER  = DATA_DIR / "kalender.json"
BACKUPS   = DATA_DIR / "backups"
UPLOADS.mkdir(parents=True, exist_ok=True)

# Ein Prozess, viele Threads (siehe Procfile) → dieses Lock serialisiert alle
# Lese-Ändere-Schreib-Zyklen auf kalender.json. NICHT auf mehrere Gunicorn-
# Worker skalieren, ohne das durch ein Datei-Lock zu ersetzen.
LOCK = threading.RLock()

# Deployment-Sicherheitsnetz: ohne DATA_DIR-Volume ist auf Railway alles
# Hochgeladene beim nächsten Deploy weg. Laut, aber nicht fatal.
if os.environ.get("PORT") and DATA_DIR == BASE:
    print("!" * 72)
    print("! WARNUNG: DATA_DIR ist nicht gesetzt — Uploads landen im Container")
    print("! und sind nach jedem Deploy/Neustart WEG. Railway-Volume mounten")
    print("! und DATA_DIR auf den Mount-Pfad setzen!")
    print("!" * 72)

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
# Uploads sind Bilder + Videos — 1 GB Obergrenze, damit ein kaputter Client
# den Server nicht mit Endlos-Bodies flutet. Flask antwortet dann mit 413.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024

PLATFORMS = ["Instagram", "TikTok", "Fanvue", "Facebook", "Threads"]

_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,8}$")


# ── Atomare Datei-Helfer ─────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str) -> None:
    """Erst in Temp-Datei schreiben, dann atomar umbenennen — ein Crash mitten
    im Schreiben kann so nie mehr eine halbe/kaputte JSON-Datei hinterlassen."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load():
    if DATA.exists():
        return json.loads(DATA.read_text())
    return {"personas": ["Luna", "Mia", "Zara"], "posts": []}


def save(d):
    _atomic_write(DATA, json.dumps(d, ensure_ascii=False, indent=2))


def _media_ref_count(obj) -> int:
    """Wie viele /uploads/-Referenzen stecken im Zustand? Grundlage für den
    Wipe-Guard: ein Speichern, das massiv Referenzen verliert, ist verdächtig."""
    try:
        return json.dumps(obj).count("/uploads/")
    except Exception:
        return 0


def _load_cal():
    if KALENDER.exists():
        try:
            cal = json.loads(KALENDER.read_text())
        except json.JSONDecodeError:
            # Korrupte Datei → letztes Backup nehmen statt mit None den
            # Client in den Default-Zustand (und damit in den Wipe) zu schicken.
            cal = _latest_backup()
        if isinstance(cal, dict):
            cal.setdefault("_revs", {})
            return cal
    return None


def _latest_backup():
    if not BACKUPS.exists():
        return None
    for f in sorted(BACKUPS.glob("kalender-*.json"), reverse=True):
        try:
            cal = json.loads(f.read_text())
            if isinstance(cal, dict):
                return cal
        except json.JSONDecodeError:
            continue
    return None


_last_backup_ts = 0.0

def _save_cal(cal: dict) -> None:
    """Unter LOCK aufrufen. Schreibt Backup (rate-limitiert) + Zustand atomar."""
    global _last_backup_ts
    import time
    now = time.time()
    if KALENDER.exists() and now - _last_backup_ts >= 60:
        BACKUPS.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        _atomic_write(BACKUPS / f"kalender-{stamp}.json", KALENDER.read_text())
        _last_backup_ts = now
        # Rotation: die letzten 50 behalten — plus das jeweils erste Backup
        # eines Tages (30 Tage), damit ein schleichender Datenverlust nicht
        # nach ein paar Stunden aus der Historie rotiert ist.
        all_backups = sorted(BACKUPS.glob("kalender-*.json"))
        daily_keep = {}
        for f in all_backups:
            day = f.name[9:17]  # kalender-YYYYMMDD-…
            daily_keep.setdefault(day, f)  # erstes Backup des Tages
        keep = set(all_backups[-50:]) | set(list(daily_keep.values())[-30:])
        for f in all_backups:
            if f not in keep:
                f.unlink()
    _atomic_write(KALENDER, json.dumps(cal, ensure_ascii=False, indent=2))


# ── Basis-Routen ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(BASE), "index.html")


@app.route("/data", methods=["GET"])
def get_data():
    return jsonify(load())


@app.route("/personas", methods=["POST"])
def add_persona_legacy():
    d = load()
    name = request.json.get("name", "").strip()
    if name and name not in d["personas"]:
        d["personas"].append(name)
        save(d)
    return jsonify(d["personas"])


@app.route("/personas/<name>", methods=["DELETE"])
def del_persona_legacy(name):
    d = load()
    d["personas"] = [p for p in d["personas"] if p != name]
    save(d)
    return jsonify(d["personas"])


# ── Kalender: Lesen ──────────────────────────────────────────────────────────

@app.route("/kalender", methods=["GET"])
def get_kalender():
    with LOCK:
        return jsonify(_load_cal())


# ── Kalender: Pro-Persona-Speichern (der neue, sichere Pfad) ────────────────
#
# Der Client schickt NUR die Tage der Persona, an der er gerade arbeitet,
# plus die Revisionsnummer, auf der sein Stand basiert. Der Server merged das
# unter Lock in den Gesamtzustand. Ergebnis: Creator A auf Luna und Creator B
# auf Mia können sich gar nicht mehr gegenseitig überschreiben. Arbeiten zwei
# Leute an DERSELBEN Persona, gewinnt der erste; der zweite bekommt 409 samt
# aktuellem Serverstand und lädt neu, statt still Daten zu zerstören.

@app.route("/kalender/persona/<name>", methods=["PUT"])
def put_persona_days(name):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("days"), list):
        return jsonify({"error": "days fehlt"}), 400
    with LOCK:
        cal = _load_cal()
        if cal is None:
            return jsonify({"error": "Kalender existiert noch nicht — Seite neu laden"}), 409
        revs = cal.setdefault("_revs", {})
        cur_rev = int(revs.get(name, 0))
        client_rev = int(body.get("rev", -1))
        if name in cal.get("byPersona", {}) and client_rev != cur_rev:
            return jsonify({
                "error": "Stand veraltet — jemand anderes hat diese Persona geändert",
                "days": cal["byPersona"][name].get("days", []),
                "rev": cur_rev,
            }), 409
        cal.setdefault("byPersona", {}).setdefault(name, {})["days"] = body["days"]
        if name not in cal.get("personas", []):
            cal.setdefault("personas", []).append(name)
        revs[name] = cur_rev + 1
        _save_cal(cal)
        return jsonify({"ok": True, "rev": revs[name]})


@app.route("/kalender/persona/<name>", methods=["DELETE"])
def delete_persona(name):
    with LOCK:
        cal = _load_cal()
        if cal is None:
            return jsonify({"error": "Kalender existiert noch nicht"}), 409
        personas = cal.get("personas", [])
        if name not in personas:
            return jsonify({"ok": True, "personas": personas})
        if len(personas) <= 1:
            return jsonify({"error": "Letzte Persona kann nicht gelöscht werden"}), 400
        cal["personas"] = [p for p in personas if p != name]
        cal.get("byPersona", {}).pop(name, None)
        cal.get("_revs", {}).pop(name, None)
        for key in ("personaPhotos", "personaPhotoPos", "personaEmojis"):
            if isinstance(cal.get(key), dict):
                cal[key].pop(name, None)
        _save_cal(cal)
        return jsonify({"ok": True, "personas": cal["personas"]})


@app.route("/kalender/meta", methods=["POST"])
def kalender_meta():
    """Kleine, gezielte Meta-Operationen statt Vollzustand-Überschreiben."""
    body = request.get_json(silent=True) or {}
    op = body.get("op", "")
    with LOCK:
        cal = _load_cal()
        if cal is None:
            return jsonify({"error": "Kalender existiert noch nicht"}), 409
        if op == "addPersona":
            name = str(body.get("name", "")).strip()
            if not name:
                return jsonify({"error": "Name fehlt"}), 400
            if name not in cal.setdefault("personas", []):
                cal["personas"].append(name)
            cal.setdefault("byPersona", {}).setdefault(name, {"days": body.get("days", [])})
            cal.setdefault("_revs", {}).setdefault(name, 0)
        elif op == "setPhoto":
            cal.setdefault("personaPhotos", {})[body.get("name", "")] = body.get("url", "")
        elif op == "setPhotoPos":
            cal.setdefault("personaPhotoPos", {})[body.get("name", "")] = body.get("pos", 20)
        elif op == "setEmoji":
            cal.setdefault("personaEmojis", {})[body.get("name", "")] = body.get("emoji", "")
        else:
            return jsonify({"error": f"unbekannte op {op!r}"}), 400
        _save_cal(cal)
        return jsonify({"ok": True, "personas": cal.get("personas", []),
                        "_revs": cal.get("_revs", {})})


# ── Kalender: Legacy-Vollspeichern (nur noch mit Wipe-Guard) ────────────────

@app.route("/kalender", methods=["POST"])
def save_kalender():
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict) or "byPersona" not in incoming:
        # None/kaputt/leer wird NIE mehr über einen bestehenden Kalender
        # geschrieben — genau so sind früher alle Bilder "verschwunden".
        return jsonify({"error": "Ungültiger Kalender-Zustand — nicht gespeichert"}), 400
    force = request.args.get("force") == "1"
    with LOCK:
        current = _load_cal()
        if current is not None and not force:
            new_refs, cur_refs = _media_ref_count(incoming), _media_ref_count(current)
            if new_refs < cur_refs:
                # Ein alter Browser-Tab mit veraltetem Stand versucht, den
                # Kalender zu überschreiben und würde Medien verlieren → ablehnen.
                return jsonify({
                    "error": ("Veralteter Stand — dieses Speichern hätte "
                              f"{cur_refs - new_refs} Medien entfernt. Bitte Seite neu laden."),
                }), 409
            incoming.setdefault("_revs", current.get("_revs", {}))
        _save_cal(incoming)
        return jsonify({"ok": True})


# ── Debug / Wartung ─────────────────────────────────────────────────────────

@app.route("/debug/disk")
def debug_disk():
    import shutil as _sh
    total, used, free = _sh.disk_usage(DATA_DIR)
    def _dirsize(p):
        return sum(f.stat().st_size for f in p.glob("**/*") if f.is_file()) if p.exists() else 0
    return jsonify({
        "volume_total_mb": round(total / 1e6, 1),
        "volume_used_mb": round(used / 1e6, 1),
        "volume_free_mb": round(free / 1e6, 1),
        "uploads_mb": round(_dirsize(UPLOADS) / 1e6, 1),
        "backups_mb": round(_dirsize(BACKUPS) / 1e6, 1),
        "kalender_json_mb": round((KALENDER.stat().st_size if KALENDER.exists() else 0) / 1e6, 2),
        "backups_count": len(list(BACKUPS.glob("kalender-*.json"))) if BACKUPS.exists() else 0,
    })

@app.route("/debug/cleanup-backups", methods=["POST"])
def debug_cleanup_backups():
    """Volumen zu voll (z.B. weil ein Kalenderstand mal riesig war und ueber
    Wochen mitgesichert wurde) -> hier die Backup-Historie hart einkuerzen,
    ohne den aktuellen Kalender oder die Uploads anzufassen."""
    keep = max(1, min(int(request.args.get("keep", 5)), 50))
    if not BACKUPS.exists():
        return jsonify({"deleted": 0, "kept": 0})
    with LOCK:
        all_backups = sorted(BACKUPS.glob("kalender-*.json"))
        to_delete = all_backups[:-keep] if len(all_backups) > keep else []
        freed = sum(f.stat().st_size for f in to_delete)
        for f in to_delete:
            f.unlink()
    return jsonify({"deleted": len(to_delete), "kept": len(all_backups) - len(to_delete),
                     "freed_mb": round(freed / 1e6, 2)})

@app.route("/debug/extract-inline-media", methods=["POST"])
def debug_extract_inline_media():
    """Manche alten Eintraege haben ein Bild als data:-URI direkt im JSON statt
    als Datei in uploads/ (z.B. durch einen frueheren Client-Bug). Das blaeht
    kalender.json und jedes einzelne Backup davon massiv auf. Extrahiert alle
    gefundenen data:-URIs in echte Dateien und ersetzt sie durch /uploads/-Links."""
    import base64, re as _re, uuid as _uuid
    with LOCK:
        cal = _load_cal()
        if cal is None:
            return jsonify({"error": "Kalender existiert noch nicht"}), 409
        converted = []
        for persona, pd in cal.get("byPersona", {}).items():
            for day in pd.get("days", []):
                for key in ("bilder", "story", "video"):
                    slot = day.get(key)
                    if not slot:
                        continue
                    for f in slot.get("files", []):
                        u = f.get("url", "")
                        if not u.startswith("data:"):
                            continue
                        m = _re.match(r"data:[^;]+;base64,(.*)", u, _re.S)
                        if not m:
                            continue
                        raw = base64.b64decode(m.group(1))
                        ext = ".png" if "png" in u[:30] else (".jpg" if "jpeg" in u[:30] else ".bin")
                        name = f"{_uuid.uuid4()}{ext}"
                        (UPLOADS / name).write_bytes(raw)
                        f["url"] = f"/uploads/{name}"
                        converted.append({"persona": persona, "file": name, "bytes": len(raw)})
        if converted:
            _save_cal(cal)
    return jsonify({"converted": converted, "count": len(converted)})

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
    with LOCK:
        cal = _load_cal()
    if cal:
        for persona in cal.get("byPersona", {}).values():
            for day in persona.get("days", []):
                for key in ("bilder", "story", "video"):
                    slot = day.get(key)
                    if not slot:
                        continue
                    for f in slot.get("files", []):
                        u = f.get("url", "")
                        if u.startswith("/uploads/"):
                            referenced.add(u.split("/")[-1])
        for u in (cal.get("personaPhotos") or {}).values():
            if isinstance(u, str) and u.startswith("/uploads/"):
                referenced.add(u.split("/")[-1])
    orphans = sorted(f.name for f in UPLOADS.iterdir() if f.is_file() and f.name not in referenced)
    items = "".join(
        f'<div style="display:inline-block;margin:8px;text-align:center;vertical-align:top">'
        f'<img src="/uploads/{o}" loading="lazy" style="max-width:180px;max-height:180px;border-radius:8px;display:block;object-fit:cover">'
        f'<div style="font-size:11px;color:#888;word-break:break-all;max-width:180px;margin-top:4px">{o}</div>'
        f'</div>'
        for o in orphans
    )
    return (f"<html><body style='background:#111;color:#eee;font-family:sans-serif;padding:20px'>"
            f"<h2>{len(orphans)} nicht zugeordnete Bilder</h2>"
            f"<p>Diese Dateien liegen noch auf dem Server, sind aber in keinem Kalendertag mehr verlinkt.</p>"
            f"{items}</body></html>")


# ── Uploads ─────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Keine Datei empfangen"}), 400
    ext = Path(f.filename).suffix.lower() or ".png"
    if not _SAFE_EXT.match(ext):
        ext = ".bin"
    name = str(uuid.uuid4()) + ext
    dest = UPLOADS / name
    try:
        f.save(str(dest))
    except Exception as e:  # Platte voll, Volume weg, …
        return jsonify({"error": f"Speichern fehlgeschlagen: {e.__class__.__name__}"}), 500
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "Datei kam leer an — bitte erneut versuchen"}), 500
    return jsonify({"url": f"/uploads/{name}", "size": dest.stat().st_size})


@app.route("/uploads/<filename>")
def media(filename):
    # Dateinamen sind UUIDs und ändern sich nie → aggressiv cachen. Das
    # entlastet den Server massiv (90+ Bilder pro Seitenaufbau) und macht
    # "Bilder laden nicht" durch Server-Überlast deutlich unwahrscheinlicher.
    resp = send_from_directory(str(UPLOADS), filename, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


if __name__ == "__main__":
    import socket
    port = int(os.environ.get("PORT", 5056))
    local_ip = socket.gethostbyname(socket.gethostname())
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    print("✓ Content Kalender läuft auf:")
    print(f"  Dieser Mac:    http://localhost:{port}")
    print(f"  Andere Geräte: http://{local_ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
