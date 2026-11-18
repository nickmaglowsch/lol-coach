"""Posição estimada de um jogador em qualquer instante.

Âncoras usadas (por jogador), em ordem de tempo:
- frames de ~60s (posição exata naquele instante);
- mortes como VÍTIMA (posição exata do evento) -> a partir daí o jogador está morto;
- kills como MATADOR (posição do evento; o matador está a no máximo o alcance dele, ~<1000 unidades);
- âncoras extras (só Match-V5): quem matou/assistiu um épico, uma torre ou uma placa estava ali naquele instante;
  quem comprou um item estava na base (fonte), exceto logo depois de morrer (compra feita morto).
Entre âncoras: interpolação linear. Depois de morrer: 'morto' até o próximo frame
(o tempo de respawn não está nos dados) e, no próximo frame, a posição registrada.
"""
from __future__ import annotations

import bisect


class Positions:
    def __init__(self, frames: list[tuple[int, dict]], kills: list[tuple[int, int, int, float, float]],
                 extra: list | None = None):
        """frames: [(ts, {pid: (x, y)})]; kills: [(ts, victim, killer, x, y)]; extra: [(ts, pid, x, y, tipo)]."""
        self.anchors: dict[int, list[tuple[int, float, float, str]]] = {}
        for ts, pos in frames:
            for pid, xy in pos.items():
                if xy and xy[0] is not None:
                    self.anchors.setdefault(pid, []).append((ts, xy[0], xy[1], "frame"))
        for ts, victim, killer, x, y in kills:
            if x is None:
                continue
            self.anchors.setdefault(victim, []).append((ts, x, y, "death"))
            if killer and killer != victim:
                self.anchors.setdefault(killer, []).append((ts, x, y, "kill"))
        for ts, pid, x, y, kind in extra or []:
            if x is not None:
                self.anchors.setdefault(pid, []).append((ts, x, y, kind))
        for a in self.anchors.values():
            a.sort(key=lambda t: t[0])
        self._ts = {pid: [t[0] for t in a] for pid, a in self.anchors.items()}

    def at(self, pid: int, ts: int) -> dict | None:
        """{'x','y','dead': bool, 'source': 'frame'|'interp'|'death'} ou None."""
        a = self.anchors.get(pid)
        if not a:
            return None
        i = bisect.bisect_right(self._ts[pid], ts)
        if i == 0:
            t, x, y, _ = a[0]
            return {"x": x, "y": y, "dead": False, "source": "frame"}
        prev = a[i - 1]
        if prev[3] == "death":
            return {"x": prev[1], "y": prev[2], "dead": True, "source": "death", "deathTs": prev[0]}
        if prev[0] == ts or i == len(a):
            return {"x": prev[1], "y": prev[2], "dead": False, "source": prev[3]}
        nxt = a[i]
        r = (ts - prev[0]) / max(1, nxt[0] - prev[0])
        return {"x": prev[1] + r * (nxt[1] - prev[1]), "y": prev[2] + r * (nxt[2] - prev[2]),
                "dead": False, "source": "interp"}


def from_summary(s: dict) -> Positions:
    frames = [(r["ts"], {int(pid): (v["x"], v["y"]) for pid, v in r["players"].items()}) for r in s["perMinute"]]
    kills = [(k["ts"], k["victim"], k["killer"], k["x"], k["y"]) for k in s["events"]["kills"]]
    return Positions(frames, kills, [tuple(a) for a in s["events"].get("anchors") or []])


FOUNTAIN = {100: (400, 400), 200: (14400, 14400)}   # landmarks.py: "fonte azul"/"fonte vermelha"


def extra_anchors(events: list[dict], team_of: dict[int, int]) -> list[list]:
    """Âncoras dos eventos da Match-V5: [ts, pid, x, y, tipo]. Compras só valem se o jogador não morreu nos 60 s
    anteriores (morto também compra, e o tempo de renascer não está nos dados)."""
    out, deaths = [], {}
    for e in events:
        if e.get("type") == "CHAMPION_KILL":
            deaths.setdefault(e.get("victimId"), []).append(e.get("timestamp", 0))
    for e in events:
        ty, ts = e.get("type"), e.get("timestamp", 0)
        pos = e.get("position") or {}
        if ty in ("ELITE_MONSTER_KILL", "BUILDING_KILL", "TURRET_PLATE_DESTROYED") and pos.get("x") is not None:
            kind = {"ELITE_MONSTER_KILL": "objetivo", "BUILDING_KILL": "estrutura", "TURRET_PLATE_DESTROYED": "placa"}[ty]
            for pid in [e.get("killerId")] + list(e.get("assistingParticipantIds") or []):
                if pid in team_of:
                    out.append([ts, pid, pos["x"], pos["y"], kind])
        elif ty == "ITEM_PURCHASED" and e.get("participantId") in team_of and ts > 0:
            pid = e["participantId"]
            if any(0 <= ts - d <= 60000 for d in deaths.get(pid, [])):
                continue
            x, y = FOUNTAIN[team_of[pid]]
            out.append([ts, pid, x, y, "base"])
    out.sort(key=lambda a: (a[1], a[0]))
    # várias compras seguidas na base: uma âncora por visita
    dedup = []
    for a in out:
        if dedup and a[4] == "base" and dedup[-1][4] == "base" and dedup[-1][1] == a[1] and a[0] - dedup[-1][0] <= 30000:
            continue
        dedup.append(a)
    return dedup
