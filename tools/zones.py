"""Classificação de posições do Summoner's Rift (mapId 11) em zonas.

Coordenadas: x,y em unidades do jogo, origem no canto da base azul (time 100, canto inferior esquerdo);
base vermelha (time 200) no canto superior direito. O mapa é aproximadamente simétrico pela
transformação (x, y) -> (14800 - x, 14900 - y) (derivada dos pares de torres dos dois times).

Zonas absolutas: base_blue, base_red, top, mid, bot, river, jungle_blue, jungle_red.
Zonas relativas (para um time): base_ally, base_enemy, top, mid, bot, river, jungle_ally, jungle_enemy.

Ordem de prioridade: base > lanes > rio > jungle. Constantes calibradas e validadas com
`python tools/zones.py validate` (veja CLAUDE.md para os números obtidos).
"""
from __future__ import annotations

import math

MIRROR_X, MIRROR_Y = 14800, 14900
DIAG_C = 14850               # x + y no centro do mapa (linha do rio)

# Lanes como polilinhas (pontos de referência a partir de torres e cantos do mapa)
TOP = [(1200, 4600), (1350, 13450), (10200, 13700)]
BOT = [(MIRROR_X - x, MIRROR_Y - y) for (x, y) in TOP]          # (13600,10300) -> (13450,1450) -> (4600,1200)
MID = [(3900, 3900), (10900, 11000)]
SIDE_LANE_W = 1000           # meia-largura (distância máx. à polilinha) das lanes laterais
MID_LANE_W = 850             # meia-largura da mid
RIVER_HALF = 1300            # |x + y - DIAG_C| máximo para ser rio
PITS = [(5007, 10471), (9866, 4414)]   # barão/arauto (topo) e dragão (baixo)
PIT_R = 1000


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _poly_dist(px, py, poly) -> float:
    return min(_seg_dist(px, py, *poly[i], *poly[i + 1]) for i in range(len(poly) - 1))


def _in_blue_base(x, y) -> bool:
    return x + y < 7400 and max(x, y) < 4600


def zone_abs(x: float, y: float) -> str:
    if _in_blue_base(x, y):
        return "base_blue"
    if _in_blue_base(MIRROR_X - x, MIRROR_Y - y):
        return "base_red"
    d = {"top": _poly_dist(x, y, TOP), "bot": _poly_dist(x, y, BOT), "mid": _poly_dist(x, y, MID)}
    cands = [(v, k) for k, v in d.items() if v <= (MID_LANE_W if k == "mid" else SIDE_LANE_W)]
    if cands:
        return min(cands)[1]
    if abs(x + y - DIAG_C) <= RIVER_HALF or any(math.hypot(x - a, y - b) <= PIT_R for a, b in PITS):
        return "river"
    return "jungle_blue" if x + y < DIAG_C else "jungle_red"


def zone_rel(x: float, y: float, team: int) -> str:
    z = zone_abs(x, y)
    if z.startswith(("base_", "jungle_")):
        kind, side = z.split("_")
        mine = (side == "blue") == (team == 100)
        return f"{kind}_{'ally' if mine else 'enemy'}"
    return z


ZONE_PT = {
    "top": "top", "mid": "mid", "bot": "bot", "river": "rio",
    "jungle_ally": "jungle aliada", "jungle_enemy": "jungle inimiga",
    "base_ally": "base aliada", "base_enemy": "base inimiga",
    "jungle_blue": "jungle azul", "jungle_red": "jungle vermelha",
    "base_blue": "base azul", "base_red": "base vermelha",
}


def side_of(x: float, y: float) -> str:
    """Metade do mapa (lado azul/vermelho), útil para kills em lane."""
    return "blue" if x + y < DIAG_C else "red"


# ----------------------------------------------------------------------------- validação
def _validate() -> None:
    """Valida a classificação com rótulos independentes dos dados em cache:
    1) torres OUTER/INNER destruídas devem cair na lane do laneType do evento; NEXUS_TURRET na base;
    2) junglers (Smite) entre 2:00 e 14:00 devem estar majoritariamente em jungle/rio;
    3) frame 0 (spawn) deve ser base."""
    import collections
    import glob
    import json
    import os
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "matches")
    lane_map = {"TOP_LANE": "top", "MID_LANE": "mid", "BOT_LANE": "bot"}
    tw_ok = tw_n = nx_ok = nx_n = sp_ok = sp_n = 0
    tw_bad = []
    jg = collections.Counter()
    games = 0
    for d in glob.glob(os.path.join(root, "*")):
        mp, tp = os.path.join(d, "match.json"), os.path.join(d, "timeline.json")
        if not (os.path.exists(mp) and os.path.exists(tp)):
            continue
        m = json.load(open(mp, encoding="utf-8"))
        if m.get("mapId") != 11:
            continue
        games += 1
        t = json.load(open(tp, encoding="utf-8"))
        smite = {p["participantId"]: p["teamId"] for p in m["participants"] if 11 in (p["spell1Id"], p["spell2Id"])}
        for i, f in enumerate(t["frames"]):
            for pid, pf in f["participantFrames"].items():
                pos = pf.get("position") or {}
                if "x" not in pos:
                    continue
                if i == 0:
                    sp_n += 1
                    sp_ok += zone_abs(pos["x"], pos["y"]).startswith("base")
                if int(pid) in smite and 2 <= i <= 14:
                    jg[zone_rel(pos["x"], pos["y"], smite[int(pid)])] += 1
            for e in f["events"]:
                if e["type"] != "BUILDING_KILL":
                    continue
                x, y = e["position"]["x"], e["position"]["y"]
                z = zone_abs(x, y)
                if e.get("towerType") in ("OUTER_TURRET", "INNER_TURRET"):
                    tw_n += 1
                    if z == lane_map.get(e["laneType"]):
                        tw_ok += 1
                    else:
                        tw_bad.append((e["towerType"], e["laneType"], x, y, z))
                elif e.get("towerType") == "NEXUS_TURRET":
                    nx_n += 1
                    nx_ok += z.startswith("base")
    tot = sum(jg.values()) or 1
    camps = {"blue buff": (3821, 8101), "gromp": (2288, 8448), "lobos": (3780, 6443), "aves": (6943, 5422),
             "red buff": (7765, 4020), "krugs": (8394, 2641)}
    rio_pts = {"scuttle topo": (4400, 9600), "scuttle baixo": (10500, 5100), "barão": PITS[0], "dragão": PITS[1]}
    c_ok = sum(zone_abs(x, y) == "jungle_blue" and zone_abs(MIRROR_X - x, MIRROR_Y - y) == "jungle_red"
               for x, y in camps.values())
    r_ok = sum(zone_abs(x, y) == "river" for x, y in rio_pts.values())
    print(f"Campos de jungle (azul e espelho vermelho) na jungle certa: {c_ok}/{len(camps)}")
    print(f"Scuttles e fossos de barão/dragão no rio: {r_ok}/{len(rio_pts)}")
    print(f"Partidas SR analisadas: {games}")
    print(f"Torres outer/inner na lane certa: {tw_ok}/{tw_n}")
    for b in sorted(set(tw_bad)):
        print("   erro:", b)
    print(f"Torres do nexus classificadas como base: {nx_ok}/{nx_n}")
    print(f"Spawn (frame 0) em base: {sp_ok}/{sp_n}")
    print("Junglers (Smite) 2:00-14:00 por zona:")
    for k, v in jg.most_common():
        print(f"   {ZONE_PT.get(k, k):<15} {v:>5}  {100 * v / tot:5.1f}%")
    jr = (jg["jungle_ally"] + jg["jungle_enemy"] + jg["river"]) / tot
    print(f"   => jungle+rio: {100 * jr:.1f}%")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        _validate()
    elif len(sys.argv) == 3:
        print(zone_abs(float(sys.argv[1]), float(sys.argv[2])))
    else:
        print("uso: python tools/zones.py validate | python tools/zones.py <x> <y>")
