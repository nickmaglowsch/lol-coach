"""Pontos de referência do Summoner's Rift (mapId 11) para descrever posições.

Torres e inibidores: coordenadas EXATAS, extraídas dos eventos BUILDING_KILL das partidas em cache
(cada uma apareceu de 1 a 17 vezes, sempre na mesma coordenada).
Campos, fossos, scuttles e fontes: coordenadas APROXIMADAS (centro do campo).

Uso: python tools/landmarks.py <x> <y>   -> zona + 3 referências mais próximas
"""
from __future__ import annotations

import math

T = "torre"
# (nome, x, y, time dono [100 azul, 200 vermelho, 0 neutro], tipo, exato?)
LANDMARKS: list[tuple[str, int, int, int, str, bool]] = [
    # time azul (100)
    ("torre externa top azul", 981, 10441, 100, T, True),
    ("torre interna top azul", 1512, 6699, 100, T, True),
    ("torre base top azul", 1169, 4287, 100, T, True),
    ("inibidor top azul", 1172, 3583, 100, "inibidor", True),
    ("torre externa mid azul", 5846, 6396, 100, T, True),
    ("torre interna mid azul", 5048, 4812, 100, T, True),
    ("torre base mid azul", 3651, 3696, 100, T, True),
    ("inibidor mid azul", 3210, 3217, 100, "inibidor", True),
    ("torre externa bot azul", 10504, 1029, 100, T, True),
    ("torre interna bot azul", 6919, 1483, 100, T, True),
    ("torre base bot azul", 4281, 1253, 100, T, True),
    ("inibidor bot azul", 3468, 1230, 100, "inibidor", True),
    ("torre do nexus azul (1)", 2177, 1807, 100, T, True),
    ("torre do nexus azul (2)", 1748, 2270, 100, T, True),
    ("fonte azul", 400, 400, 100, "fonte", False),
    # time vermelho (200)
    ("torre externa top vermelha", 4318, 13875, 200, T, True),
    ("torre interna top vermelha", 7943, 13411, 200, T, True),
    ("torre base top vermelha", 10481, 13650, 200, T, True),
    ("inibidor top vermelho", 11275, 13657, 200, "inibidor", True),
    ("torre externa mid vermelha", 8955, 8510, 200, T, True),
    ("torre interna mid vermelha", 9767, 10113, 200, T, True),
    ("torre base mid vermelha", 11134, 11207, 200, T, True),
    ("inibidor mid vermelho", 11593, 11669, 200, "inibidor", True),
    ("torre externa bot vermelha", 13866, 4505, 200, T, True),
    ("torre interna bot vermelha", 13327, 8226, 200, T, True),
    ("torre base bot vermelha", 13624, 10572, 200, T, True),
    ("inibidor bot vermelho", 13599, 11319, 200, "inibidor", True),
    ("torre do nexus vermelha (1)", 13052, 12612, 200, T, True),
    ("torre do nexus vermelha (2)", 12611, 13084, 200, T, True),
    ("fonte vermelha", 14400, 14400, 200, "fonte", False),
    # neutros
    ("fosso do barão/arauto/vastilarvas", 5007, 10471, 0, "fosso", False),
    ("fosso do dragão", 9866, 4414, 0, "fosso", False),
    ("scuttle do rio top", 4400, 9600, 0, "scuttle", False),
    ("scuttle do rio bot", 10500, 5100, 0, "scuttle", False),
]
_CAMPS_BLUE = [("blue buff", 3821, 8101), ("gromp", 2288, 8448), ("lobos", 3780, 6443),
               ("aves", 6943, 5422), ("red buff", 7765, 4020), ("krugs", 8394, 2641)]
for _n, _x, _y in _CAMPS_BLUE:
    LANDMARKS.append((f"{_n} azul", _x, _y, 100, "campo", False))
    LANDMARKS.append((f"{_n} vermelho", 14800 - _x, 14900 - _y, 200, "campo", False))

TOWERS = [l for l in LANDMARKS if l[4] == T]


def nearest(x: float, y: float, n: int = 1, kind: str | None = None) -> list[dict]:
    pts = [l for l in LANDMARKS if kind is None or l[4] == kind]
    pts = sorted(pts, key=lambda l: math.hypot(x - l[1], y - l[2]))[:n]
    return [{"name": l[0], "dist": int(math.hypot(x - l[1], y - l[2])), "team": l[3], "kind": l[4]} for l in pts]


def describe(x: float, y: float) -> str:
    """Texto curto: 'torre externa bot vermelha ~390'."""
    if x is None or y is None:
        return ""
    l = nearest(x, y)[0]
    return f'{l["name"]} ~{l["dist"]}'


if __name__ == "__main__":
    import sys
    from zones import ZONE_PT, zone_abs
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 3:
        raise SystemExit("uso: python tools/landmarks.py <x> <y>")
    x, y = float(sys.argv[1]), float(sys.argv[2])
    print(f"zona\t{ZONE_PT.get(zone_abs(x, y), zone_abs(x, y))}")
    for l in nearest(x, y, 3):
        print(f'{l["name"]}\t{l["dist"]}')
