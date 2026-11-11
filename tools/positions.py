"""Posição estimada de um jogador em qualquer instante.

Âncoras usadas (por jogador), em ordem de tempo:
- frames de ~60s (posição exata naquele instante);
- mortes como VÍTIMA (posição exata do evento) -> a partir daí o jogador está morto;
- kills como MATADOR (posição do evento; o matador está a no máximo o alcance dele, ~<1000 unidades).
Entre âncoras: interpolação linear. Depois de morrer: 'morto' até o próximo frame
(o tempo de respawn não está nos dados) e, no próximo frame, a posição registrada.
"""
from __future__ import annotations

import bisect


class Positions:
    def __init__(self, frames: list[tuple[int, dict]], kills: list[tuple[int, int, int, float, float]]):
        """frames: [(ts, {pid: (x, y)})]; kills: [(ts, victim, killer, x, y)]."""
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
    return Positions(frames, kills)
