"""Partidas sintéticas de Summoner's Rift no formato Match-V5, para testar sem dados reais.

  python tools/fixture.py [pasta]     gera <pasta>/{matches,static,me.json} e imprime a pasta no stdout
                                      (sem pasta: cria uma temporária)
  LOL_DATA_DIR=<pasta> python tools/query.py players 9100000003

Nada aqui vem de partidas reais: nomes ("Teste1#BR1"…), PUUIDs, ids (91000000xx) e números são inventados a
partir de um roteiro fixo e de um sorteio com semente fixa (a saída é a mesma a cada execução).
São 3 partidas com os mesmos 10 jogadores; "me" (me.json) é Teste2#BR1, Lee Sin, jungle do time azul (pid 2):
  9100000003  Flex (440), 10/11/2026, vitória do "me" — a principal (a mais recente)
  9100000002  Solo/Duo (420), 09/11/2026, o mesmo roteiro espelhado (lados trocados): derrota do "me"
  9100000001  Normal (400), 08/11/2026, o roteiro da primeira com outro sorteio e um 2º barão: vitória
O roteiro tem abates (com lutas de 3+ abates em 20 s), dragões, vastilarvas, arauto, barão, placas, torres,
inibidor, recalls com compras, wards e level-ups, para exercitar summarize/query/render/decisions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from landmarks import LANDMARKS  # noqa: E402  (coordenadas exatas de torres e inibidores)

C = 14850                       # x + y no rio; espelhar = refletir na diagonal do rio (top continua top)
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# pid -> (championId, nome pt-BR, nome interno da API)
CHAMPS = {1: (31, "Cho'Gath", "Chogath"), 2: (64, "Lee Sin", "LeeSin"), 3: (103, "Ahri", "Ahri"),
          4: (222, "Jinx", "Jinx"), 5: (412, "Thresh", "Thresh"), 6: (122, "Darius", "Darius"),
          7: (121, "Kha'Zix", "Khazix"), 8: (238, "Zed", "Zed"), 9: (51, "Caitlyn", "Caitlyn"),
          10: (117, "Lulu", "Lulu")}
SPELLS = {"TOP": (4, 12), "JUNGLE": (4, 11), "MIDDLE": (4, 14), "BOTTOM": (4, 7), "UTILITY": (4, 3)}
ITEMS = {1054: ("Escudo de Doran", 450), 1055: ("Lâmina de Doran", 450), 1056: ("Anel de Doran", 400),
         1102: ("Filhote de Mascote da Selva", 450), 3865: ("Atlas Mundial", 400), 2003: ("Poção de Vida", 50),
         2055: ("Sentinela de Controle", 75), 1001: ("Botas", 300), 1036: ("Espada Longa", 350),
         1052: ("Tomo Amplificador", 400), 1028: ("Cristal de Rubi", 400), 1029: ("Armadura de Pano", 300),
         1037: ("Picareta", 875), 1038: ("Espada B.F.", 1300), 1026: ("Cajado Explosivo", 850),
         1058: ("Bastão Desnecessariamente Grande", 1200), 3006: ("Grevas do Berserker", 800),
         3020: ("Sapatos do Feiticeiro", 800), 3047: ("Botas Galvanizadas", 900), 3071: ("Cutelo Negro", 3000),
         6672: ("Matadora de Krakens", 3000), 3031: ("Gume do Infinito", 3450), 3089: ("Capuz da Morte", 3600),
         6655: ("Tormento de Luden", 2750), 3068: ("Égide de Fogo Solar", 2700), 3075: ("Armadura de Espinhos", 2450),
         3190: ("Medalhão de Ferro", 2200), 3142: ("Lâmina Fantasma", 2800)}
STARTER = {"TOP": [1054, 2003], "JUNGLE": [1102], "MIDDLE": [1056, 2003, 2003], "BOTTOM": [1055, 2003],
           "UTILITY": [3865, 2003, 2003]}
BUILD = {"TOP": [1028, 1001, 2055, 1029, 3047, 3068, 2055, 3075, 1028],
         "JUNGLE": [1036, 1001, 2055, 1037, 3047, 3071, 2055, 3142, 1036],
         "MIDDLE": [1052, 1001, 1026, 3020, 6655, 1058, 3089, 1052],
         "BOTTOM": [1036, 1001, 1037, 3006, 6672, 1038, 3031, 1036],
         "UTILITY": [2055, 1029, 1001, 2055, 3190, 2055, 1028, 3047, 2055]}
CS_RATE = {"TOP": 7.0, "JUNGLE": 5.6, "MIDDLE": 7.6, "BOTTOM": 8.2, "UTILITY": 1.1}     # por minuto
XP_RATE = {"TOP": 470, "JUNGLE": 420, "MIDDLE": 480, "BOTTOM": 380, "UTILITY": 330}
DMG_RATE = {"TOP": 420, "JUNGLE": 380, "MIDDLE": 620, "BOTTOM": 650, "UTILITY": 260}
HP_BASE = {"TOP": 650, "JUNGLE": 620, "MIDDLE": 590, "BOTTOM": 610, "UTILITY": 600}
XP_LEVEL = [0, 280, 660, 1140, 1720, 2400, 3180, 4060, 5040, 6120, 7300, 8580, 9960, 11440, 13020, 14700, 16480, 18360]

FOUNTAIN = {100: (400, 400), 200: (14400, 14500)}
LANE_EARLY = {100: {"TOP": (1400, 12000), "MIDDLE": (6900, 6950), "BOTTOM": (12300, 1650), "UTILITY": (12000, 1900)},
              200: {"TOP": (2300, 13300), "MIDDLE": (7900, 7950), "BOTTOM": (13250, 2600), "UTILITY": (13000, 2900)}}
LANE_LATE = {100: {"TOP": (2500, 13000), "MIDDLE": (6500, 6600), "BOTTOM": (7200, 7100), "UTILITY": (7600, 6900)},
             200: {"TOP": (3600, 13600), "MIDDLE": (8300, 8400), "BOTTOM": (13400, 5000), "UTILITY": (9000, 8600)}}
CAMPS = [(7765, 4020), (8394, 2641), (6943, 5422), (3780, 6443), (3821, 8101), (2288, 8448)]  # azul; vermelho = espelho
PIT = {"DRAGON": (9866, 4414), "BARON_NASHOR": (5007, 10471), "RIFTHERALD": (5007, 10471), "HORDE": (4790, 10182)}

# ----------------------------------------------------------------------------- roteiro (partida principal)
# Abates: (segundo, matador, vítima, assistências, (x, y))
KILLS = [
    (200, 2, 6, [1], (1700, 12700)),            # 03:20 gank do Lee Sin no top
    (370, 9, 4, [10, 7], (12500, 1900)),        # 06:10 luta no bot (3 abates em 15 s)
    (378, 2, 10, [5], (12600, 2300)),
    (385, 7, 5, [9], (12800, 2900)),
    (580, 3, 8, [2], (7200, 7500)),             # 09:40 mid
    (660, 7, 1, [6], (1400, 11200)),            # 11:00 top
    (1155, 3, 8, [2, 4], (8300, 7900)),         # 19:15 luta grande no meio
    (1160, 8, 5, [7], (8100, 7600)),
    (1166, 4, 9, [3, 2], (8600, 8000)),
    (1172, 2, 7, [1, 4], (8800, 8300)),
    (1480, 1, 6, [2, 3], (5600, 10900)),        # 24:40 luta do barão
    (1488, 4, 10, [5], (5300, 10600)),
    (1500, 9, 3, [8], (5200, 10300)),
    (1630, 6, 1, [7], (11000, 11800)),          # 27:10 defesa na base vermelha
    (1660, 4, 8, [3, 5], (11400, 11500)),
]
# Monstros épicos: (segundo, matador, tipo, subtipo, assistências)
ELITES = [
    (330, 7, "DRAGON", "FIRE_DRAGON", [9, 10]),
    (500, 2, "HORDE", None, [3]), (503, 2, "HORDE", None, [3]), (507, 3, "HORDE", None, [2]),
    (710, 2, "DRAGON", "WATER_DRAGON", [4, 5]),
    (990, 7, "RIFTHERALD", None, [8]),
    (1070, 2, "DRAGON", "EARTH_DRAGON", [3, 4, 5]),
    (1430, 2, "DRAGON", "AIR_DRAGON", [4, 5]),
    (1515, 2, "BARON_NASHOR", None, [1, 4, 5]),
]
# Placas: (segundo, matador [0 = minions], estrutura, dono)
PLATES = [(630, 4, "torre externa bot", 200), (642, 4, "torre externa bot", 200),
          (700, 1, "torre externa top", 200), (760, 6, "torre externa top", 100), (790, 0, "torre externa mid", 200)]
# Estruturas: (segundo, matador, assistências, estrutura, dono)
BUILDINGS = [
    (850, 4, [5], "torre externa bot", 200),
    (1040, 6, [7], "torre externa top", 100),
    (1200, 3, [2], "torre externa mid", 200),
    (1320, 4, [5], "torre interna bot", 200),
    (1560, 1, [2], "torre externa top", 200),
    (1590, 3, [2, 4], "torre interna mid", 200),
    (1610, 3, [2, 4, 5], "torre base mid", 200),
    (1625, 4, [3, 5], "inibidor mid", 200),
    (1680, 4, [2, 3, 5], "torre do nexus (1)", 200),
    (1690, 3, [2, 4], "torre do nexus (2)", 200),
]
DURATION = 1710          # 28:30; o time azul (100) vence no roteiro principal
RECALL_MIN = {"TOP": [7, 12, 18, 23], "JUNGLE": [7, 13, 18, 22], "MIDDLE": [6, 12, 17, 22],
              "BOTTOM": [7, 13, 17, 22], "UTILITY": [7, 13, 17, 21]}

GAMES = [
    {"gameId": 9100000003, "seed": 11, "mirror": False, "queueId": 440, "creation": (2026, 11, 10, 21, 0)},
    {"gameId": 9100000002, "seed": 22, "mirror": True, "queueId": 420, "creation": (2026, 11, 9, 20, 30)},
    # 2º barão (do inimigo, 18:30): o histórico precisa de um intervalo entre barões para estimar o renascimento
    {"gameId": 9100000001, "seed": 33, "mirror": False, "queueId": 400, "creation": (2026, 11, 8, 19, 0),
     "extraElites": [(1110, 7, "BARON_NASHOR", None, [6, 8])]},
]
ME_PID = 2


def team_of(pid: int) -> int:
    return 100 if pid <= 5 else 200


def role_of(pid: int) -> str:
    return ROLES[(pid - 1) % 5]


def riot_id(pid: int) -> tuple[str, str]:
    return f"Teste{pid}", "BR1"


def puuid(pid: int) -> str:
    return f"fixture-puuid-{pid:02d}-" + "0" * 50


def structure_xy(name: str, owner: int) -> tuple[int, int]:
    """'torre externa bot' + dono -> coordenada exata da estrutura (landmarks.py)."""
    if name.startswith("torre do nexus"):
        full = name.replace("(", ("azul (" if owner == 100 else "vermelha ("))
    elif name.startswith("inibidor"):
        full = f'{name} {"azul" if owner == 100 else "vermelho"}'
    else:
        full = f'{name} {"azul" if owner == 100 else "vermelha"}'
    hit = next(l for l in LANDMARKS if l[0] == full)
    return hit[1], hit[2]


def level_of(xp: float) -> int:
    return sum(1 for need in XP_LEVEL if xp >= need)


def respawn_s(t: float) -> float:
    return min(60.0, 8 + 1.5 * t / 60)


# ----------------------------------------------------------------------------- geração
class Game:
    def __init__(self, g: dict) -> None:
        self.g = g
        self.rng = random.Random(g["seed"])
        self.mirror = g["mirror"]
        self.events: list[dict] = []           # eventos da timeline (ms)
        self.over: dict[tuple[int, int], tuple[int, int]] = {}   # (pid, minuto) -> posição imposta pelo roteiro
        self.script_times: dict[int, list[float]] = {p: [] for p in CHAMPS}
        self.deaths: dict[int, list[tuple[float, float]]] = {p: [] for p in CHAMPS}

    # roteiro, com os lados trocados na partida espelhada
    def pid(self, p: int) -> int:
        return (p + 5 if p <= 5 else p - 5) if self.mirror else p

    def team(self, t: int) -> int:
        return 300 - t if self.mirror else t

    def xy(self, x: float, y: float) -> tuple[int, int]:
        return (int(C - y), int(C - x)) if self.mirror else (int(x), int(y))

    def jit(self, xy, r: int) -> tuple[int, int]:
        return (int(min(14700, max(100, xy[0] + self.rng.randint(-r, r)))),
                int(min(14800, max(100, xy[1] + self.rng.randint(-r, r)))))

    def involve(self, t: float, pids, xy, victim: int | None = None) -> None:
        """Quem participa de um evento aparece perto dele no frame anterior (e no seguinte, se for logo antes)."""
        m0 = int(t // 60)
        dt_ = t - m0 * 60
        for p in pids:
            if p == 0:
                continue
            self.script_times[p].append(t)
            ang = self.rng.uniform(0, 2 * math.pi)
            d = min(1600, 150 + dt_ * 25)
            self.over[(p, m0)] = self.jit((xy[0] + d * math.cos(ang), xy[1] + d * math.sin(ang)), 60)
            if dt_ > 45 and p != victim:
                self.over[(p, m0 + 1)] = self.jit(xy, 300)
        if victim:
            self.over[(victim, m0 + 1)] = self.jit(FOUNTAIN[team_of(victim)], 150)

    def build_script(self) -> None:
        for s, k, v, ass, (x, y) in KILLS:
            k, v, ass = self.pid(k), self.pid(v), [self.pid(a) for a in ass]
            t = s + self.shift + self.rng.random()
            pos = self.jit(self.xy(x, y), 150)
            self.deaths[v].append((t, t + respawn_s(t)))
            self.kills.append((t, k, v, ass, pos))
            self.involve(t, [k, v] + ass, pos, victim=v)
        for s, k, ty, sub, ass in sorted(ELITES + self.g.get("extraElites", [])):
            k, ass = self.pid(k), [self.pid(a) for a in ass]
            t = s + self.shift + self.rng.random()
            pos = self.jit(PIT[ty], 250)
            self.elites.append((t, k, ty, sub, ass, pos))
            self.involve(t, [k] + ass, pos)
        for s, k, name, owner in PLATES:
            k, owner = self.pid(k) if k else 0, self.team(owner)
            t = s + self.shift + self.rng.random()
            pos = structure_xy(name, owner)
            self.plates.append((t, k, name, owner, pos))
            self.involve(t, [k], pos)
        for s, k, ass, name, owner in BUILDINGS:
            k, ass, owner = self.pid(k), [self.pid(a) for a in ass], self.team(owner)
            t = s + self.shift + self.rng.random()
            pos = structure_xy(name, owner)
            self.buildings.append((t, k, ass, name, owner, pos))
            self.involve(t, [k] + ass, pos)

    def dead_at(self, p: int, t: float) -> bool:
        return any(a <= t < b for a, b in self.deaths[p])

    def dead_overlap(self, p: int, a: float, b: float) -> float:
        return sum(max(0.0, min(b, d1) - max(a, d0)) for d0, d1 in self.deaths[p])

    def default_pos(self, p: int, m: int) -> tuple[int, int]:
        team, role = team_of(p), role_of(p)
        if m == 0:
            return self.jit(FOUNTAIN[team], 120)
        if role == "JUNGLE":
            if (team == 100 and m == 9) or (team == 200 and m == 12):     # invasão da jungle inimiga
                cx, cy = CAMPS[(m + 2) % 6]
                pos = (cx, cy) if team == 200 else (14800 - cx, 14900 - cy)
            elif m == 4 or (m >= 14 and m % 3 == 0):                     # rio (scuttle / perto do dragão)
                pos = (10300, 5200) if team == 100 else (4500, 9700)
            else:
                cx, cy = CAMPS[(m - 1) % 6]
                pos = (cx, cy) if team == 100 else (14800 - cx, 14900 - cy)
            return self.jit(pos, 300)
        if m == 1:
            f, l = FOUNTAIN[team], LANE_EARLY[team][role]
            return self.jit(((f[0] + l[0]) / 2, (f[1] + l[1]) / 2), 300)
        return self.jit((LANE_EARLY if m < 14 else LANE_LATE)[team][role], 280)

    def plan_recalls(self) -> dict[int, list[float]]:
        """Visitas à loja: recalls planejados (sem conflito com o roteiro) e voltas depois de morrer."""
        shop: dict[int, list[float]] = {p: [] for p in CHAMPS}
        for p in CHAMPS:
            for t0, _ in self.deaths[p]:
                shop[p].append(t0 + self.rng.uniform(4, 9))
            for base in RECALL_MIN[role_of(p)]:
                for m in (base, base + 1, base - 1, base + 2):
                    t = m * 60 - self.rng.uniform(9, 15)
                    busy = any(t - 75 <= s <= m * 60 + 15 for s in self.script_times[p])
                    died = any(t - 120 <= d0 <= t for d0, _ in self.deaths[p])
                    if busy or died or (p, m) in self.over or self.dead_at(p, t) or m * 60 >= DURATION - 60:
                        continue
                    shop[p].append(t)
                    self.over[(p, m)] = self.jit(FOUNTAIN[team_of(p)], 500)
                    break
            shop[p].sort()
        return shop

    def build(self) -> tuple[dict, dict]:
        rng = self.rng
        self.shift = rng.randint(-6, 6)
        self.kills, self.elites, self.plates, self.buildings = [], [], [], []
        self.build_script()
        shop = self.plan_recalls()
        duration = DURATION + self.shift
        nmin = duration // 60                                  # frames 0..nmin a cada minuto + 1 final
        frame_ts = [m * 60000 + (rng.randint(5, 40) if m else 0) for m in range(nmin + 1)] + [duration * 1000 + 120]
        pos = {p: [self.over.get((p, m)) or self.default_pos(p, m) for m in range(nmin + 1)] for p in CHAMPS}
        for p in CHAMPS:   # no frame final: base dos vencedores/perdedores não importa; fica perto do nexus vermelho
            pos[p].append(self.jit((12000, 12200) if self.team(100) == team_of(p) else FOUNTAIN[team_of(p)], 400))

        # ---------------- ações em ordem de tempo
        acts: list[tuple[float, int, str, tuple]] = []   # (segundo, prioridade, tipo, dados)
        for p in CHAMPS:
            acts.append((15 + rng.uniform(0, 8), 0, "starter", (p,)))
            for t in shop[p]:
                acts.append((t, 1, "shop", (p,)))
        for k in self.kills:
            acts.append((k[0], 2, "kill", k))
        for e in self.elites:
            acts.append((e[0], 2, "elite", e))
        for e in self.plates:
            acts.append((e[0], 2, "plate", e))
        for e in self.buildings:
            acts.append((e[0], 2, "building", e))
        for ts in frame_ts:
            acts.append((ts / 1000, -1, "frame", (ts,)))
        acts.sort(key=lambda a: (a[0], a[1]))
        snaps: dict[int, dict] = {}

        # ---------------- estado
        st = {p: {"gold": 500.0, "cur": 500.0, "xp": 0.0, "cs": 0.0, "jcs": 0.0, "dmg": 0.0, "lvl": 1, "build": 0,
                  "k": 0, "d": 0, "a": 0, "streak": 0, "towers": 0, "inhib": 0, "drag": 0, "baron": 0,
                  "items": [], "controls": 0, "deadTime": 0.0, "firstBlood": False}
              for p in CHAMPS}
        mult = {p: rng.uniform(0.9, 1.1) for p in CHAMPS}
        cursor = {p: 0.0 for p in CHAMPS}

        def emit(t: float, ev: dict) -> None:
            ev["timestamp"] = int(t * 1000) if "timestamp" not in ev else ev["timestamp"]
            self.events.append(ev)

        def accrue(p: int, t: float) -> None:
            a, b = cursor[p], t
            if b <= a:
                return
            role, s = role_of(p), st[p]
            alive = (b - a) - self.dead_overlap(p, a, b)
            mid = (a + b) / 2 / 60
            ramp = 0.0 if mid < 1.1 else (0.5 if mid < 2 else 1.0)
            cs = CS_RATE[role] * ramp * mult[p] * alive / 60
            if role == "JUNGLE":
                s["jcs"] += cs
            else:
                s["cs"] += cs
            gold = (122.4 * max(0.0, min(b, duration) - max(a, 65)) / 60 if b > 65 else 0)
            gold += cs * (32 if role == "JUNGLE" else 21 + mid * 0.2)
            if role == "UTILITY":
                gold += 60 * ramp * alive / 60
            s["gold"] += gold
            s["cur"] += gold
            s["xp"] += XP_RATE[role] * ramp * mult[p] * (1 + mid * 0.02) * alive / 60
            s["dmg"] += DMG_RATE[role] * (0.3 if mid < 6 else 1 + mid / 25) * mult[p] * alive / 60
            s["deadTime"] += (b - a) - alive
            cursor[p] = b
            lv = min(18, level_of(s["xp"]))
            while s["lvl"] < lv:
                s["lvl"] += 1
                emit(b - rng.uniform(0, min(20.0, b - a)), {"level": s["lvl"], "participantId": p, "type": "LEVEL_UP"})

        def give(p: int, gold: float = 0, xp: float = 0) -> None:
            st[p]["gold"] += gold
            st[p]["cur"] += gold
            st[p]["xp"] += xp

        def buy(p: int, t: float, iid: int) -> None:
            st[p]["cur"] -= ITEMS[iid][1]
            st[p]["items"].append(iid)
            st[p]["controls"] += iid == 2055
            emit(t, {"itemId": iid, "participantId": p, "type": "ITEM_PURCHASED"})

        undo_done = sold_done = False
        for t, _, kind, data in acts:
            for p in CHAMPS:
                accrue(p, t)
            if kind == "frame":
                snaps[data[0]] = {p: {k: st[p][k] for k in ("gold", "cur", "xp", "cs", "jcs", "dmg", "lvl")} for p in CHAMPS}
            elif kind == "starter":
                p = data[0]
                for i, iid in enumerate(STARTER[role_of(p)]):
                    buy(p, t + 0.4 * i, iid)
            elif kind == "shop":
                p = data[0]
                s, tt = st[p], t
                if not sold_done and role_of(p) == "BOTTOM" and t > 1200:
                    sold_done = True
                    s["cur"] += 180
                    emit(tt, {"itemId": 1055, "participantId": p, "type": "ITEM_SOLD"})
                    tt += 1
                seq = BUILD[role_of(p)]
                while s["build"] < len(seq) and s["cur"] >= ITEMS[seq[s["build"]]][1]:
                    iid = seq[s["build"]]
                    buy(p, tt, iid)
                    if not undo_done and role_of(p) == "MIDDLE":       # um "desfazer" de compra, como na API
                        undo_done = True
                        st[p]["cur"] += ITEMS[iid][1]
                        st[p]["items"].pop()
                        emit(tt + 1.2, {"afterId": 0, "beforeId": iid, "goldGain": ITEMS[iid][1], "participantId": p,
                                        "type": "ITEM_UNDO"})
                        tt += 1.5
                        buy(p, tt, iid)
                    s["build"] += 1
                    tt += rng.uniform(0.8, 2.5)
                if s["cur"] >= 50 and t < 900 and rng.random() < 0.5:
                    buy(p, tt, 2003)
            elif kind == "kill":
                _, k, v, ass, (x, y) = data
                bounty = 400 if not any(st[q]["firstBlood"] for q in CHAMPS) else 300
                shutdown = 150 * (st[v]["streak"] - 1) if st[v]["streak"] >= 2 else 0
                emit(t, {"assistingParticipantIds": ass, "bounty": bounty, "killStreakLength": st[k]["streak"],
                         "killerId": k, "position": {"x": x, "y": y}, "shutdownBounty": shutdown,
                         "type": "CHAMPION_KILL", "victimId": v})
                if bounty == 400:
                    st[k]["firstBlood"] = True
                    emit(t, {"killType": "KILL_FIRST_BLOOD", "killerId": k, "position": {"x": x, "y": y},
                             "type": "CHAMPION_SPECIAL_KILL"})
                give(k, bounty + shutdown, 250)
                for a in ass:
                    give(a, (bounty + shutdown) * 0.5 / len(ass), 120)
                    st[a]["a"] += 1
                st[k]["k"] += 1
                st[k]["streak"] += 1
                st[v]["d"] += 1
                st[v]["streak"] = 0
                st[k]["dmg"] += 900
                for a in ass:
                    st[a]["dmg"] += 500
            elif kind == "elite":
                _, k, ty, sub, ass, (x, y) = data
                kteam = team_of(k)
                ev = {"assistingParticipantIds": ass, "bounty": 0, "killerId": k, "killerTeamId": kteam,
                      "monsterType": ty, "position": {"x": x, "y": y}, "type": "ELITE_MONSTER_KILL"}
                if sub:
                    ev["monsterSubType"] = sub
                emit(t, ev)
                for q in CHAMPS:
                    if team_of(q) == kteam and not self.dead_at(q, t):
                        give(q, 300 if ty == "BARON_NASHOR" else 0, 120 if q in [k] + ass else 0)
                key = {"DRAGON": "drag", "BARON_NASHOR": "baron"}.get(ty)
                if key:
                    st[k][key] += 1
            elif kind == "plate":
                _, k, name, owner, (x, y) = data
                emit(t, {"killerId": k, "laneType": name.split()[-1].upper() + "_LANE", "position": {"x": x, "y": y},
                         "teamId": owner, "type": "TURRET_PLATE_DESTROYED"})
                if k:
                    give(k, 125)
            elif kind == "building":
                _, k, ass, name, owner, (x, y) = data
                inhib = name.startswith("inibidor")
                nexus = name.startswith("torre do nexus")
                lane = "MID_LANE" if nexus else name.split()[-1].upper() + "_LANE"
                ev = {"assistingParticipantIds": ass, "bounty": 0, "buildingType": "INHIBITOR_BUILDING" if inhib else "TOWER_BUILDING",
                      "killerId": k, "laneType": lane, "position": {"x": x, "y": y}, "teamId": owner, "type": "BUILDING_KILL"}
                if not inhib:
                    ev["towerType"] = "NEXUS_TURRET" if nexus else {"externa": "OUTER_TURRET", "interna": "INNER_TURRET",
                                                                    "base": "BASE_TURRET"}[name.split()[1]]
                emit(t, ev)
                give(k, 250)
                for q in CHAMPS:
                    if team_of(q) != owner:
                        give(q, 50)
                st[k]["inhib" if inhib else "towers"] += 1
        for p in CHAMPS:
            accrue(p, duration)

        # ---------------- wards
        for p in CHAMPS:
            role = role_of(p)
            gap = 55 if role == "UTILITY" else (85 if role == "JUNGLE" else 140)
            t = 75 + rng.uniform(0, 30)
            while t < duration - 10:
                if not self.dead_at(p, t):
                    wt = "CONTROL_WARD" if (t > 360 and role in ("UTILITY", "JUNGLE") and rng.random() < 0.25) else (
                        "SIGHT_WARD" if role == "UTILITY" and t > 480 else (
                            "BLUE_TRINKET" if role == "BOTTOM" and t > 900 else "YELLOW_TRINKET"))
                    emit(t, {"creatorId": p, "type": "WARD_PLACED", "wardType": wt})
                    if role in ("UTILITY", "JUNGLE") and rng.random() < 0.45:
                        emit(t + rng.uniform(5, 40), {"killerId": p, "type": "WARD_KILL",
                                                      "wardType": rng.choice(["YELLOW_TRINKET", "SIGHT_WARD", "CONTROL_WARD"])})
                t += gap * rng.uniform(0.75, 1.3)
        winner = self.team(100)
        self.events.append({"gameId": self.g["gameId"], "realTimestamp": 0, "timestamp": duration * 1000,
                            "type": "GAME_END", "winningTeam": winner})
        self.events.sort(key=lambda e: e["timestamp"])
        return self.to_match(st, duration, winner), self.to_timeline([snaps[ts] for ts in frame_ts], pos, frame_ts)

    # ---------------- saída no formato da API
    def to_timeline(self, snap: list[dict], pos: dict, frame_ts: list[int]) -> dict:
        rng = random.Random(self.g["seed"] * 7)
        frames = []
        for i, ts in enumerate(frame_ts):
            pf = {}
            for p in CHAMPS:
                s = snap[i][p]
                lvl = s["lvl"]
                hp_max = HP_BASE[role_of(p)] + 95 * (lvl - 1)
                pw_max = 300 + 40 * (lvl - 1)
                x, y = pos[p][i]
                f = FOUNTAIN[team_of(p)]
                if self.dead_at(p, ts / 1000):
                    hp = 0
                elif math.hypot(x - f[0], y - f[1]) < 3000 or i == 0:
                    hp = hp_max
                else:
                    hp = int(hp_max * rng.uniform(0.35, 1.0))
                pf[str(p)] = {
                    "championStats": {"abilityHaste": 0, "abilityPower": 0, "armor": 30 + 4 * lvl, "attackDamage": 60 + 3 * lvl,
                                      "health": hp, "healthMax": hp_max, "magicResist": 30 + lvl,
                                      "movementSpeed": 345, "power": int(pw_max * rng.uniform(0.3, 1.0)), "powerMax": pw_max},
                    "currentGold": int(s["cur"]), "damageStats": {"totalDamageDoneToChampions": int(s["dmg"])},
                    "goldPerSecond": 0, "jungleMinionsKilled": int(s["jcs"]), "level": lvl,
                    "minionsKilled": int(s["cs"]), "participantId": p, "position": {"x": x, "y": y},
                    "timeEnemySpentControlled": 0, "totalGold": int(s["gold"]), "xp": int(s["xp"]),
                }
            frames.append({"events": [], "participantFrames": pf, "timestamp": ts})
        for e in self.events:   # frame i guarda os eventos entre o seu timestamp e o do próximo
            i = max(j for j, ts in enumerate(frame_ts) if ts <= e["timestamp"] or j == 0)
            frames[i]["events"].append(e)
        mid = f'BR1_{self.g["gameId"]}'
        return {"metadata": {"dataVersion": "2", "matchId": mid, "participants": [puuid(p) for p in CHAMPS]},
                "info": {"endOfGameResult": "GameComplete", "frameInterval": 60000, "frames": frames,
                         "gameId": self.g["gameId"], "participants": [{"participantId": p, "puuid": puuid(p)} for p in CHAMPS]}}

    def to_match(self, st: dict, duration: int, winner: int) -> dict:
        parts = []
        for p in CHAMPS:
            s, role = st[p], role_of(p)
            name, tag = riot_id(p)
            cid, _, internal = CHAMPS[p]
            placed = sum(1 for e in self.events if e["type"] == "WARD_PLACED" and e.get("creatorId") == p)
            killed = sum(1 for e in self.events if e["type"] == "WARD_KILL" and e.get("killerId") == p)
            inv = [i for i in s["items"] if ITEMS[i][1] >= 2000][-6:]
            inv += [0] * (6 - len(inv))
            parts.append({
                "participantId": p, "teamId": team_of(p), "puuid": puuid(p), "riotIdGameName": name, "riotIdTagline": tag,
                "summonerName": "", "championId": cid, "championName": internal, "teamPosition": role,
                "individualPosition": role, "lane": {"UTILITY": "BOTTOM", "BOTTOM": "BOTTOM"}.get(role, role),
                "role": "SOLO" if role in ("TOP", "MIDDLE") else ("NONE" if role == "JUNGLE" else "CARRY" if role == "BOTTOM" else "SUPPORT"),
                "summoner1Id": SPELLS[role][0], "summoner2Id": SPELLS[role][1],
                "kills": s["k"], "deaths": s["d"], "assists": s["a"], "champLevel": s["lvl"],
                "totalMinionsKilled": int(s["cs"]), "neutralMinionsKilled": int(s["jcs"]),
                "goldEarned": int(s["gold"]), "goldSpent": int(s["gold"] - s["cur"]),
                "totalDamageDealtToChampions": int(s["dmg"]), "visionScore": int(placed * 1.3 + killed * 1.6 + duration / 120),
                "wardsPlaced": placed, "wardsKilled": killed, "visionWardsBoughtInGame": s["controls"],
                "detectorWardsPlaced": sum(1 for e in self.events if e["type"] == "WARD_PLACED" and e.get("creatorId") == p
                                           and e["wardType"] == "CONTROL_WARD"),
                "turretKills": s["towers"], "inhibitorKills": s["inhib"], "dragonKills": s["drag"], "baronKills": s["baron"],
                "firstBloodKill": s["firstBlood"], "item0": inv[0], "item1": inv[1], "item2": inv[2], "item3": inv[3],
                "item4": inv[4], "item5": inv[5], "item6": 3340, "timePlayed": duration,
                "totalTimeSpentDead": int(s["deadTime"]), "win": team_of(p) == winner,
                "gameEndedInSurrender": False, "gameEndedInEarlySurrender": False,
            })

        def count(team: int, pred) -> int:
            return sum(1 for e in self.events if pred(e) and (
                (e.get("killerTeamId") == team) or (e["type"] == "BUILDING_KILL" and e["teamId"] != team) or
                (e["type"] == "CHAMPION_KILL" and team_of(e["killerId"]) == team)))
        teams = []
        for team in (100, 200):
            obj = {"atakhan": 0,
                   "baron": count(team, lambda e: e.get("monsterType") == "BARON_NASHOR"),
                   "champion": count(team, lambda e: e["type"] == "CHAMPION_KILL"),
                   "dragon": count(team, lambda e: e.get("monsterType") == "DRAGON"),
                   "horde": count(team, lambda e: e.get("monsterType") == "HORDE"),
                   "inhibitor": count(team, lambda e: e.get("buildingType") == "INHIBITOR_BUILDING"),
                   "riftHerald": count(team, lambda e: e.get("monsterType") == "RIFTHERALD"),
                   "tower": count(team, lambda e: e.get("buildingType") == "TOWER_BUILDING")}
            teams.append({"teamId": team, "win": team == winner, "bans": [],
                          "objectives": {k: {"first": False, "kills": v} for k, v in obj.items()}})
        y, mo, d, h, mi = self.g["creation"]
        created = int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone(dt.timedelta(hours=-3))).timestamp() * 1000)
        return {"metadata": {"dataVersion": "2", "matchId": f'BR1_{self.g["gameId"]}', "participants": [puuid(p) for p in CHAMPS]},
                "info": {"endOfGameResult": "GameComplete", "gameCreation": created, "gameDuration": duration,
                         "gameEndTimestamp": created + 60000 + duration * 1000, "gameId": self.g["gameId"],
                         "gameMode": "CLASSIC", "gameName": "teste", "gameStartTimestamp": created + 60000,
                         "gameType": "MATCHED_GAME", "gameVersion": "16.22.700.1234", "mapId": 11,
                         "participants": parts, "platformId": "BR1", "queueId": self.g["queueId"], "teams": teams,
                         "tournamentCode": ""}}


# ----------------------------------------------------------------------------- escrita
def _dump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def generate(data_dir: Path) -> dict:
    """Escreve as partidas, os dados estáticos mínimos e o me.json em data_dir (formato de data/)."""
    data_dir = Path(data_dir)
    for g in GAMES:
        match, timeline = Game(g).build()
        d = data_dir / "matches" / str(g["gameId"])
        _dump(d / "match_v5.json", match)
        _dump(d / "timeline_v5.json", timeline)
        _dump(d / "meta.json", {"gameId": g["gameId"], "platformId": "BR1", "riot": True, "fixture": True})
    _dump(data_dir / "static" / "champions.json",
          [{"id": cid, "name": name, "alias": alias} for cid, name, alias in CHAMPS.values()])
    _dump(data_dir / "static" / "items.json",
          [{"id": i, "name": n, "priceTotal": price, "categories": []} for i, (n, price) in ITEMS.items()])
    _dump(data_dir / "static" / "queues.json",
          [{"id": 400, "name": "Normal (Escolha Alternada)", "description": "5v5"},
           {"id": 420, "name": "Ranqueada Solo/Duo", "description": "5v5"},
           {"id": 440, "name": "Ranqueada Flexível", "description": "5v5"}])
    name, tag = riot_id(ME_PID)
    _dump(data_dir / "me.json", {"puuid": puuid(ME_PID), "gameName": name, "tagLine": tag,
                                 "riotId": f"{name}#{tag}", "platformId": "BR1", "alias": "Teste"})
    return {"dataDir": str(data_dir), "games": [g["gameId"] for g in GAMES], "main": GAMES[0]["gameId"],
            "me": f"{name}#{tag}", "mePid": ME_PID}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", nargs="?", help="pasta de dados a criar (padrão: uma pasta temporária nova)")
    a = ap.parse_args()
    out = Path(a.pasta) if a.pasta else Path(tempfile.mkdtemp(prefix="lolcoach-fixture-"))
    if (out / "matches").exists() and any((out / "matches").iterdir()) and             not all(d.name.startswith("91000000") for d in (out / "matches").iterdir()):
        raise SystemExit(f"{out} já tem partidas que não são da fixture; escolha outra pasta.")
    info = generate(out)
    print(f"{len(info['games'])} partidas sintéticas em {out} (use LOL_DATA_DIR={out})", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
