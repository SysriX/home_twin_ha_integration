"""Indoor-Ortung aus Signalstärken (reines Python, ohne HA-Abhängigkeiten).

Proxys kommen aus dem Home-Twin-Plan (Gerätetyp "bleproxy" mit Feld scanner = Bluetooth-
Adresse des Proxys, x/y in Metern, optional rssi_1m und path_loss). Pro BLE-Gerät:

  - Abstand je Proxy aus dem geglätteten RSSI (Log-Distance-Modell)
  - 1 Proxy: Position = Proxy, Genauigkeit = Abstand
  - 2 Proxys: Punkt auf der Verbindungslinie im Verhältnis der Abstände
  - ab 3 Proxys: gewichtete Trilateration (Gauss-Newton), Genauigkeit = Rest-Fehler
  - Etage = Etage des stärksten Proxys; Raum = Raumfläche, in der der Punkt liegt
"""

from __future__ import annotations

import math
import re
from typing import Any

DEFAULT_RSSI_1M = -59.0
DEFAULT_PATH_LOSS = 2.5
MIN_ACC = 0.5
MAX_DIST = 30.0


def _num(v: Any, d: float | None = None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return f if math.isfinite(f) else d


def norm_name(name: Any) -> str:
    """'FL-01 BLE-Proxy (88:57:…)' / 'fl-ble-01' -> vergleichbarer Schlüssel."""
    s = re.sub(r"\s*\([0-9A-Fa-f:]{17}\)\s*$", "", str(name or ""))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def point_in_poly(x: float, y: float, pts: list) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i][0], pts[i][1]
        xj, yj = pts[j][0], pts[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class PlanIndex:
    """Proxys und Räume aus dem Plan, für schnelle Abfragen vorbereitet."""

    def __init__(self, plan: dict | None, scanner_names: dict[str, str] | None = None) -> None:
        """scanner_names: {normierter Name: Scanner-Adresse} – Rückfall, wenn im Plan kein Scanner gewählt ist."""
        self.proxies: dict[str, dict] = {}   # Scanner-Adresse -> {floor, x, y, ref, n, name}
        sc_names = scanner_names or {}
        self.rooms: dict[str, list] = {}     # Etage -> [(room_id, name, points)]
        for fl in (plan or {}).get("floors") or []:
            if not isinstance(fl, dict) or not fl.get("id"):
                continue
            fid = str(fl["id"])
            rooms = [r for r in fl.get("rooms") or [] if isinstance(r, dict)]
            names = {r.get("id"): r.get("name") or r.get("id") for r in rooms}
            lst = []
            for r in rooms:
                pts = [p for p in r.get("points") or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(pts) >= 3:
                    rid = r.get("part_of") or r.get("id")
                    lst.append((rid, names.get(rid) or rid, [(float(p[0]), float(p[1])) for p in pts]))
            self.rooms[fid] = lst
            for d in fl.get("devices") or []:
                if not isinstance(d, dict) or d.get("type") != "bleproxy":
                    continue
                src = d.get("scanner") or sc_names.get(norm_name(d.get("name")))
                if not src:
                    continue
                x, y = _num(d.get("x")), _num(d.get("y", d.get("true")))  # "y" kann aus altem YAML als true kommen
                if x is None or y is None:
                    continue
                self.proxies[str(src).upper()] = {
                    "floor": fid, "x": x, "y": y, "name": d.get("name"),
                    "ref": _num(d.get("rssi_1m")), "n": _num(d.get("path_loss"), DEFAULT_PATH_LOSS) or DEFAULT_PATH_LOSS,
                }

    def room_at(self, floor: str, x: float, y: float) -> tuple[str | None, str | None]:
        for rid, name, pts in self.rooms.get(floor, []):
            if point_in_poly(x, y, pts):
                return rid, name
        return None, None


def distance(rssi: float, ref: float, n: float) -> float:
    return min(MAX_DIST, max(0.1, 10 ** ((ref - rssi) / (10 * n))))


def locate(idx: PlanIndex, obs: dict[str, float], tag_ref: float | None = None) -> dict | None:
    """obs = {scanner_adresse: geglätteter RSSI}. Ergebnis {f, x, y, acc, room, rn, n} oder None."""
    pts = []
    for src, rssi in obs.items():
        p = idx.proxies.get(src.upper())
        if p is None or rssi is None:
            continue
        ref = tag_ref if tag_ref is not None else (p["ref"] if p["ref"] is not None else DEFAULT_RSSI_1M)
        pts.append((p, rssi, distance(rssi, ref, p["n"])))
    if not pts:
        return None
    floor = max(pts, key=lambda t: t[1])[0]["floor"]
    pts = [t for t in pts if t[0]["floor"] == floor]
    pts.sort(key=lambda t: t[2])

    if len(pts) == 1:
        p, _, d = pts[0]
        x, y, acc = p["x"], p["y"], max(MIN_ACC, d)
    elif len(pts) == 2:
        (p1, _, d1), (p2, _, d2) = pts
        f = d1 / (d1 + d2)
        x, y = p1["x"] + (p2["x"] - p1["x"]) * f, p1["y"] + (p2["y"] - p1["y"]) * f
        base = math.hypot(p2["x"] - p1["x"], p2["y"] - p1["y"])
        acc = max(MIN_ACC, abs(d1 + d2 - base) / 2 + 0.5)
    else:
        x, y, acc = _trilaterate(pts[:6])

    rid, rn = idx.room_at(floor, x, y)
    return {"f": floor, "x": round(x, 2), "y": round(y, 2), "acc": round(acc, 2), "room": rid, "rn": rn, "n": len(pts)}


def _trilaterate(pts: list) -> tuple[float, float, float]:
    """Gewichtete Kleinste Quadrate: min Σ w_i (|p - P_i| - d_i)², w = 1/d²."""
    w = [1 / max(d, 0.3) ** 2 for _, _, d in pts]
    sw = sum(w)
    x = sum(p["x"] * wi for (p, _, _), wi in zip(pts, w)) / sw
    y = sum(p["y"] * wi for (p, _, _), wi in zip(pts, w)) / sw
    for _ in range(25):
        jtj = [[0.0, 0.0], [0.0, 0.0]]
        jtr = [0.0, 0.0]
        for (p, _, d), wi in zip(pts, w):
            dx, dy = x - p["x"], y - p["y"]
            r = math.hypot(dx, dy) or 1e-6
            jx, jy, res = dx / r, dy / r, r - d
            jtj[0][0] += wi * jx * jx
            jtj[0][1] += wi * jx * jy
            jtj[1][1] += wi * jy * jy
            jtr[0] += wi * jx * res
            jtr[1] += wi * jy * res
        jtj[1][0] = jtj[0][1]
        det = jtj[0][0] * jtj[1][1] - jtj[0][1] ** 2
        if abs(det) < 1e-12:
            break
        sx = (jtj[1][1] * jtr[0] - jtj[0][1] * jtr[1]) / det
        sy = (jtj[0][0] * jtr[1] - jtj[1][0] * jtr[0]) / det
        x, y = x - sx, y - sy
        if abs(sx) + abs(sy) < 1e-3:
            break
    rms = math.sqrt(sum(wi * (math.hypot(x - p["x"], y - p["y"]) - d) ** 2 for (p, _, d), wi in zip(pts, w)) / sw)
    return x, y, max(MIN_ACC, rms)
