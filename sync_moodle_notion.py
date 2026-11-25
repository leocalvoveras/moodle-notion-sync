#!/usr/bin/env python3
"""Sincroniza el calendario iCal de Moodle con una base de datos de Notion.

- Crea en Notion las tareas nuevas (Estado inicial: "Sin empezar").
- Si cambia el plazo (o el nombre) en Moodle, lo actualiza en Notion.
- Nunca toca el Estado de una tarea que ya existe.

Variables de entorno necesarias (secretos de GitHub):
  MOODLE_ICS_URL      URL de exportación del calendario de Moodle
  NOTION_TOKEN        Token de tu integración de Notion
  NOTION_DATABASE_ID  ID de la base de datos de Notion
Opcional:
  DRY_RUN=1           Solo muestra lo que haría, sin escribir en Notion
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MOODLE_ICS_URL = os.environ["MOODLE_ICS_URL"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"

TZ = ZoneInfo("Europe/Madrid")
NOTION_VERSION = "2022-06-28"

# Nombres de las propiedades en tu base de datos de Notion
P_TITLE = "Nombre de la tarea"   # tipo Título
P_SUBJECT = "Asignatura"         # tipo Selección
P_STATUS = "Estado"              # tipo Selección
P_DATE = "Plazo"                 # tipo Fecha
P_ID = "ID Moodle"               # tipo Texto (hay que crearla)

# Cada patrón se busca tanto en el nombre del curso de Moodle como en las
# opciones de Asignatura de Notion (sin tildes ni mayúsculas). Si coinciden,
# se asigna esa opción. Así no importa si hay pequeñas diferencias de nombre.
# Segundo valor: nombre a usar si esa opción aún no existe en Notion
# (Notion la crea sola). None = no crear nada.
SUBJECT_PATTERNS = [
    (r"marcas", None),
    (r"digitaliz", None),
    (r"entornos", None),
    (r"ingles", None),
    (r"sistemas inform", None),
    (r"programacion", None),
    (r"bases? de datos", "Base de Datos"),
]

# Textos que Moodle añade al título de las tareas y que sobran en Notion
TITLE_SUFFIXES = [" está en fecha de entrega", " vence", " is due", " closes", " opens"]

# Cursos de Moodle que no cursas: sus tareas no se importan nunca
# (patrones sobre el nombre del curso, sin tildes ni guiones)
IGNORE_COURSES = [r"empleabilidad", r"\bipe\b"]

# Prefijos que Moodle añade al título y sobran en Notion
TITLE_PREFIXES = ["Vencimiento de ", "Cierre de "]

# Eventos que no son tareas y no se importan (patrones sobre el título)
IGNORE_TITLES = [r"^informativo"]

# No se crean tareas cuyo plazo ya pasó hace más de estos días
SKIP_OLDER_THAN_DAYS = 0


def norm(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


# ---------- Moodle / iCal ----------

def fetch_ics():
    req = urllib.request.Request(MOODLE_ICS_URL, headers={"User-Agent": "moodle-notion-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def unescape(value):
    return (value.replace("\\n", " ").replace("\\N", " ")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_events(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]  # línea continuada
        else:
            lines.append(raw)
    events, cur = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, value = line.split(":", 1)
            key = name.split(";")[0].upper()
            cur[key] = (name, unescape(value))
    return events


def parse_dt(params, value):
    """Devuelve la fecha en formato ISO (con hora y zona, o solo fecha)."""
    if "VALUE=DATE" in params.upper() or len(value) == 8:
        return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(TZ)
    else:
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=TZ)
    return dt.isoformat()


def clean_title(summary):
    title = summary.strip()
    for prefix in TITLE_PREFIXES:
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
            break
    for suffix in TITLE_SUFFIXES:
        if title.lower().endswith(suffix.strip().lower()):
            title = title[: -len(suffix.strip())].rstrip()
            break
    return title or summary.strip()


def same_moment(a, b):
    if not a or not b:
        return a == b
    try:
        if len(a) == 10 or len(b) == 10:
            return a[:10] == b[:10]
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return a == b


# ---------- Notion ----------

def notion(method, path, body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(
            "https://api.notion.com/v1" + path,
            method=method,
            data=data,
            headers={
                "Authorization": "Bearer " + NOTION_TOKEN,
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            sys.exit(f"Error de Notion {e.code}: {e.read().decode()[:500]}")


def load_schema():
    db = notion("GET", f"/databases/{DATABASE_ID}")
    props = db["properties"]
    expected = {P_TITLE: "title", P_SUBJECT: "select",
                P_DATE: "date", P_ID: "rich_text"}
    for name, kind in expected.items():
        if name not in props:
            sys.exit(f"Falta la propiedad '{name}' (tipo {kind}) en la base de datos de Notion.")
        if props[name]["type"] != kind:
            sys.exit(f"La propiedad '{name}' debe ser de tipo {kind}, pero es {props[name]['type']}.")
    if P_STATUS not in props:
        sys.exit(f"Falta la propiedad '{P_STATUS}' en la base de datos de Notion.")
    status_kind = props[P_STATUS]["type"]  # puede ser "select" o "status"
    if status_kind not in ("select", "status"):
        sys.exit(f"La propiedad '{P_STATUS}' debe ser de tipo select o status, pero es {status_kind}.")
    subject_options = [o["name"] for o in props[P_SUBJECT]["select"]["options"]]
    status_options = [o["name"] for o in props[P_STATUS][status_kind]["options"]]
    initial = next((o for o in status_options if norm(o).startswith("sin")), "Sin empezar")
    return subject_options, initial, status_kind


def load_existing():
    pages, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"/databases/{DATABASE_ID}/query", body)
        for p in res["results"]:
            rt = p["properties"].get(P_ID, {}).get("rich_text", [])
            uid = "".join(t["plain_text"] for t in rt)
            if uid:
                pages[uid] = p
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def match_subject(course, options):
    course_n = norm(course).replace("-", " ")
    for pattern, default_name in SUBJECT_PATTERNS:
        if re.search(pattern, course_n):
            for opt in options:
                if re.search(pattern, norm(opt)):
                    return opt
            return default_name
    return None


# ---------- Principal ----------

def main():
    subject_options, initial_status, status_kind = load_schema()
    existing = load_existing()
    events = parse_events(fetch_ics())
    limit = (datetime.now(TZ) - timedelta(days=SKIP_OLDER_THAN_DAYS)).date().isoformat()

    created = updated = unchanged = skipped = 0
    for ev in events:
        uid = ev.get("UID", ("", ""))[1]
        summary = ev.get("SUMMARY", ("", ""))[1]
        if not uid or not summary or "DTSTART" not in ev:
            continue
        title = clean_title(summary)
        if any(re.search(pat, title, re.IGNORECASE) for pat in IGNORE_TITLES):
            continue
        due = parse_dt(*ev["DTSTART"])
        course = ev.get("CATEGORIES", ("", ""))[1]
        course_n = norm(course).replace("-", " ")
        if any(re.search(pat, course_n) for pat in IGNORE_COURSES):
            continue
        subject = match_subject(course, subject_options) if course else None
        if course and not subject:
            print(f"Aviso: no sé qué asignatura es '{course}' (tarea: {title})")

        page = existing.get(uid)
        if page is None:
            if due[:10] < limit:
                skipped += 1
                continue
            props = {
                P_TITLE: {"title": [{"text": {"content": title[:2000]}}]},
                P_ID: {"rich_text": [{"text": {"content": uid}}]},
                P_DATE: {"date": {"start": due}},
                P_STATUS: {status_kind: {"name": initial_status}},
            }
            if subject:
                props[P_SUBJECT] = {"select": {"name": subject}}
            print(f"[crear] {title} -> {due}")
            if not DRY_RUN:
                notion("POST", "/pages", {"parent": {"database_id": DATABASE_ID}, "properties": props})
                time.sleep(0.35)
            created += 1
            continue

        p = page["properties"]
        old_title = "".join(t["plain_text"] for t in p[P_TITLE]["title"])
        old_date = (p[P_DATE]["date"] or {}).get("start")
        old_subject = (p[P_SUBJECT]["select"] or {}).get("name")
        changes = {}
        if old_title != title:
            changes[P_TITLE] = {"title": [{"text": {"content": title[:2000]}}]}
        if not same_moment(old_date, due):
            changes[P_DATE] = {"date": {"start": due}}
        if not old_subject and subject:
            changes[P_SUBJECT] = {"select": {"name": subject}}
        if not changes:
            unchanged += 1
            continue
        print(f"[actualizar] {title}: {old_date} -> {due}" if P_DATE in changes else f"[actualizar] {title}")
        if not DRY_RUN:
            notion("PATCH", f"/pages/{page['id']}", {"properties": changes})
            time.sleep(0.35)
        updated += 1

    modo = " (simulación, no se ha escrito nada)" if DRY_RUN else ""
    print(f"Listo{modo}: {created} creadas, {updated} actualizadas, "
          f"{unchanged} sin cambios, {skipped} antiguas omitidas.")


if __name__ == "__main__":
    main()
