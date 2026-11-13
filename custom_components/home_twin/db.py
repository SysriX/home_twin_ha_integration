"""Datenbank-Schicht für Home Twin (synchron, läuft im Executor).

Bewusst ohne Home-Assistant-Importe, damit sie auch außerhalb von HA testbar ist.
Der Plan wird als JSON-Dokument mit Revisionen gespeichert; zusätzlich werden
Etagen, Räume, Elemente und Ausschnitte bei jedem Speichern in eigene Tabellen
projiziert, damit Grafana & Co. sie direkt abfragen können.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine, make_url

SCHEMA_VERSION = 1

LongText = Text().with_variant(mysql.LONGTEXT(), "mysql", "mariadb")
TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mariadb_engine": "InnoDB",
    "mariadb_charset": "utf8mb4",
}

md = MetaData()

t_meta = Table(
    "ht_meta", md,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
    **TABLE_KW,
)

t_plan = Table(
    "ht_plan", md,
    Column("plan_id", String(64), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("data", LongText, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("updated_by", String(255)),
    **TABLE_KW,
)

t_revision = Table(
    "ht_revision", md,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("plan_id", String(64), nullable=False, index=True),
    Column("revision", Integer, nullable=False),
    Column("data", LongText, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("user_id", String(64)),
    Column("user_name", String(255)),
    Column("note", String(255)),
    Column("size", Integer),
    UniqueConstraint("plan_id", "revision", name="uq_ht_revision"),
    **TABLE_KW,
)

t_floor = Table(
    "ht_floor", md,
    Column("plan_id", String(64), primary_key=True),
    Column("floor_id", String(64), primary_key=True),
    Column("name", String(255)),
    Column("level", Integer),
    Column("ha_floor_id", String(64)),
    **TABLE_KW,
)

t_room = Table(
    "ht_room", md,
    Column("plan_id", String(64), primary_key=True),
    Column("floor_id", String(64), primary_key=True),
    Column("room_id", String(64), primary_key=True),
    Column("name", String(255)),
    Column("area_id", String(64)),
    Column("part_of", String(64)),
    Column("floor_type", String(32)),
    Column("area_m2", Float),
    Column("cx", Float),
    Column("cy", Float),
    Column("points", Text),
    **TABLE_KW,
)

t_item = Table(
    "ht_item", md,
    Column("plan_id", String(64), primary_key=True),
    Column("floor_id", String(64), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("item_id", String(64)),
    Column("item_type", String(32)),
    Column("entity_id", String(255), index=True),
    Column("x", Float),
    Column("y", Float),
    Column("room_id", String(64)),
    Column("data", Text),
    **TABLE_KW,
)

t_view = Table(
    "ht_view", md,
    Column("plan_id", String(64), primary_key=True),
    Column("view_id", String(64), primary_key=True),
    Column("name", String(255)),
    Column("floor_id", String(64)),
    Column("x", Float),
    Column("y", Float),
    Column("w", Float),
    Column("h", Float),
    Column("rotate", Integer),
    **TABLE_KW,
)

PROJECTION_TABLES = (t_floor, t_room, t_item, t_view)

# Hinzugefügte BLE-Tags (nur diese bekommen HA-Geräte/Sensoren)
t_ble_tag = Table(
    "ht_ble_tag", md,
    Column("address", String(17), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("kind", String(32)),
    Column("icon", String(64)),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("data", Text),
    **TABLE_KW,
)


class ConflictError(Exception):
    """Plan wurde zwischenzeitlich von jemand anderem gespeichert."""

    def __init__(self, current: int) -> None:
        super().__init__(f"Plan wurde zwischenzeitlich geändert (Revision {current})")
        self.current = current


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


# ---------------------------------------------------------------- Geometrie
def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN raus


def _pts(points: Any) -> list[tuple[float, float]]:
    out = []
    for p in points or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = _num(p[0]), _num(p[1])
            if x is not None and y is not None:
                out.append((x, y))
    return out


def poly_area(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def point_in_poly(x: float, y: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _s(v: Any, n: int) -> str | None:
    if v is None or v == "":
        return None
    return str(v)[:n]


def _slim(item: dict) -> str:
    """Element als JSON ohne eingebettete Bilder (data:-URIs)."""
    d = {k: ("<data-uri>" if isinstance(v, str) and v.startswith("data:") else v) for k, v in item.items()}
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def project(plan_id: str, plan: dict) -> dict[Table, list[dict]]:
    """Plan-Dokument in Tabellenzeilen zerlegen."""
    rows: dict[Table, list[dict]] = {t: [] for t in PROJECTION_TABLES}
    seen_floors = set()
    for fl in plan.get("floors") or []:
        if not isinstance(fl, dict):
            continue
        fid = _s(fl.get("id"), 64)
        if not fid or fid in seen_floors:
            continue
        seen_floors.add(fid)
        rows[t_floor].append({
            "plan_id": plan_id, "floor_id": fid, "name": _s(fl.get("name"), 255),
            "level": int(_num(fl.get("level")) or 0), "ha_floor_id": _s(fl.get("ha_floor"), 64),
        })
        rooms = []
        seen_rooms = set()
        for r in fl.get("rooms") or []:
            if not isinstance(r, dict):
                continue
            rid = _s(r.get("id"), 64)
            if not rid or rid in seen_rooms:
                continue
            seen_rooms.add(rid)
            pts = _pts(r.get("points"))
            rooms.append((rid, r, pts))
            rows[t_room].append({
                "plan_id": plan_id, "floor_id": fid, "room_id": rid, "name": _s(r.get("name"), 255),
                "area_id": _s(r.get("area") or r.get("area_id"), 64), "part_of": _s(r.get("part_of"), 64),
                "floor_type": _s(r.get("floor"), 32),
                "area_m2": round(poly_area(pts), 3) if len(pts) >= 3 else None,
                "cx": round(sum(p[0] for p in pts) / len(pts), 3) if pts else None,
                "cy": round(sum(p[1] for p in pts) / len(pts), 3) if pts else None,
                "points": json.dumps(pts),
            })

        def room_of(x: float | None, y: float | None) -> str | None:
            if x is None or y is None:
                return None
            for rid, r, pts in rooms:
                if len(pts) >= 3 and point_in_poly(x, y, pts):
                    return _s(r.get("part_of"), 64) or rid
            return None

        for kind, key in (("opening", "openings"), ("device", "devices"), ("furniture", "furniture")):
            for idx, it in enumerate(fl.get(key) or []):
                if not isinstance(it, dict):
                    continue
                x, y = _num(it.get("x")), _num(it.get("y"))
                rows[t_item].append({
                    "plan_id": plan_id, "floor_id": fid, "kind": kind, "idx": idx,
                    "item_id": _s(it.get("id"), 64),
                    "item_type": _s(it.get("type") or it.get("symbol") or ("image" if it.get("src") else None), 32),
                    "entity_id": _s(it.get("entity"), 255), "x": x, "y": y, "room_id": room_of(x, y),
                    "data": _slim(it),
                })
    seen_views = set()
    for v in plan.get("views") or []:
        if not isinstance(v, dict):
            continue
        vid = _s(v.get("id"), 64)
        if not vid or vid in seen_views:
            continue
        seen_views.add(vid)
        rows[t_view].append({
            "plan_id": plan_id, "view_id": vid, "name": _s(v.get("name"), 255), "floor_id": _s(v.get("floor"), 64),
            "x": _num(v.get("x")), "y": _num(v.get("y")), "w": _num(v.get("w")), "h": _num(v.get("h")),
            "rotate": int(_num(v.get("rotate")) or 0),
        })
    return rows


# ---------------------------------------------------------------- Datenbank
class TwinDB:
    """Zugriff auf die Home-Twin-Datenbank (MariaDB, zum Testen auch SQLite)."""

    def __init__(self, url: str, keep_revisions: int = 200) -> None:
        self.url = url
        self.keep = max(1, int(keep_revisions))
        self.engine: Engine | None = None

    # -- Verbindung
    def connect(self) -> None:
        u = make_url(self.url)
        kw: dict[str, Any] = {"pool_pre_ping": True, "future": True}
        if u.get_backend_name() in ("mysql", "mariadb"):
            kw.update(pool_recycle=1800, pool_size=2, max_overflow=3, connect_args={"connect_timeout": 10})
        self.engine = create_engine(u, **kw)
        self.init_schema()

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    def init_schema(self) -> int:
        md.create_all(self.engine)
        with self.engine.begin() as c:
            v = c.execute(select(t_meta.c.value).where(t_meta.c.key == "schema_version")).scalar()
            if v is None:
                c.execute(insert(t_meta).values(key="schema_version", value=str(SCHEMA_VERSION)))
                return SCHEMA_VERSION
            return int(v)

    def ping(self) -> None:
        with self.engine.connect() as c:
            c.execute(select(1))

    # -- Lesen
    def get_revision_no(self, plan_id: str) -> int | None:
        with self.engine.connect() as c:
            return c.execute(select(t_plan.c.revision).where(t_plan.c.plan_id == plan_id)).scalar()

    def get_plan(self, plan_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(t_plan).where(t_plan.c.plan_id == plan_id)).mappings().first()
        if r is None:
            return None
        return {
            "revision": r["revision"], "plan": json.loads(r["data"]),
            "updated_at": _iso(r["updated_at"]), "updated_by": r["updated_by"],
        }

    def list_revisions(self, plan_id: str, limit: int = 50) -> list[dict]:
        q = (
            select(t_revision.c.revision, t_revision.c.created_at, t_revision.c.user_name,
                   t_revision.c.note, t_revision.c.size)
            .where(t_revision.c.plan_id == plan_id)
            .order_by(t_revision.c.revision.desc())
            .limit(limit)
        )
        with self.engine.connect() as c:
            return [
                {"revision": r.revision, "created_at": _iso(r.created_at), "user_name": r.user_name,
                 "note": r.note, "size": r.size}
                for r in c.execute(q)
            ]

    def get_revision(self, plan_id: str, revision: int) -> dict | None:
        q = select(t_revision).where(t_revision.c.plan_id == plan_id, t_revision.c.revision == revision)
        with self.engine.connect() as c:
            r = c.execute(q).mappings().first()
        if r is None:
            return None
        return {"revision": r["revision"], "plan": json.loads(r["data"]), "created_at": _iso(r["created_at"]),
                "user_name": r["user_name"], "note": r["note"]}

    # -- Schreiben
    def save_plan(
        self,
        plan_id: str,
        plan: dict,
        base_revision: int | None,
        force: bool = False,
        user_id: str | None = None,
        user_name: str | None = None,
        note: str | None = None,
    ) -> dict:
        data = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
        now = _now()
        rows = project(plan_id, plan)
        with self.engine.begin() as c:
            cur = c.execute(
                select(t_plan.c.revision).where(t_plan.c.plan_id == plan_id).with_for_update()
            ).scalar()
            if not force and base_revision is not None and base_revision != (cur or 0):
                raise ConflictError(cur or 0)
            new = (cur or 0) + 1
            vals = {"revision": new, "data": data, "updated_at": now, "updated_by": _s(user_name, 255)}
            if cur is None:
                c.execute(insert(t_plan).values(plan_id=plan_id, **vals))
            else:
                c.execute(update(t_plan).where(t_plan.c.plan_id == plan_id).values(**vals))
            c.execute(insert(t_revision).values(
                plan_id=plan_id, revision=new, data=data, created_at=now, user_id=_s(user_id, 64),
                user_name=_s(user_name, 255), note=_s(note, 255), size=len(data.encode()),
            ))
            for t in PROJECTION_TABLES:
                c.execute(delete(t).where(t.c.plan_id == plan_id))
                if rows[t]:
                    c.execute(insert(t), rows[t])
            c.execute(delete(t_revision).where(
                t_revision.c.plan_id == plan_id, t_revision.c.revision <= new - self.keep
            ))
        return {"revision": new, "updated_at": _iso(now), "updated_by": user_name}

    # -- BLE-Tags
    def list_tags(self) -> dict[str, dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(t_ble_tag)).mappings().all()
        return {r["address"]: {"name": r["name"], "kind": r["kind"], "icon": r["icon"],
                               "created": _iso(r["created_at"]), **json.loads(r["data"] or "{}")} for r in rows}

    def upsert_tag(self, address: str, tag: dict) -> None:
        now = _now()
        extra = {k: v for k, v in tag.items() if k not in ("name", "kind", "icon", "created")}
        vals = {"name": _s(tag.get("name"), 255) or address, "kind": _s(tag.get("kind"), 32),
                "icon": _s(tag.get("icon"), 64), "updated_at": now, "data": json.dumps(extra, ensure_ascii=False)}
        with self.engine.begin() as c:
            hit = c.execute(update(t_ble_tag).where(t_ble_tag.c.address == address).values(**vals)).rowcount
            if not hit:
                c.execute(insert(t_ble_tag).values(address=address, created_at=now, **vals))

    def delete_tag(self, address: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(t_ble_tag).where(t_ble_tag.c.address == address))

    def stats(self, plan_id: str) -> dict:
        with self.engine.connect() as c:
            return {
                t.name: c.execute(select(func.count()).select_from(t).where(t.c.plan_id == plan_id)).scalar()
                for t in (t_revision, *PROJECTION_TABLES)
            }
