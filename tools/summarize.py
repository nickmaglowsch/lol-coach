"""Gera resumos analisáveis a partir dos dados brutos em data/matches/.

Uso:
  python tools/summarize.py <gameId>                    # gera data/summaries/<gameId>.json e imprime o resumo
  python tools/summarize.py <gameId> --focus "Nome#TAG" # destaca outro jogador (padrão: a conta do cliente)
  python tools/summarize.py <gameId> --quiet            # só gera o JSON
  python tools/summarize.py --player "Nome#TAG" --games 10 [--queue 440]   # padrões em várias partidas
  python tools/summarize.py --all                       # (re)gera o resumo de todas as partidas em cache

Somente leitura do cache; nada é baixado aqui (use tools/fetch.py antes).
Fonte preferida por partida: timeline_v5.json (Match-V5, mais rica) se existir; senão timeline.json (LCU).
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from collections import Counter, defaultdict
from itertools import permutations

from common import (DATA, MATCHES, SUMMARIES, load_json, me_puuid, mmss, queue_name,
                    resolve_player, save_json)
from landmarks import describe as landmark
from positions import Positions
from zones import ZONE_PT, zone_rel

STATIC = DATA / "static"
SUMMARY_VERSION = 4
NEAR_OBJ = 2500     # raio (unidades) para "perto do objetivo" (estimado)
NEAR_KILL = 2000    # raio para aliados/inimigos perto de uma kill (estimado)
ROLE_PT = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "suporte", "": "?"}
ZONE_SHORT = {"top": "TOP", "mid": "MID", "bot": "BOT", "river": "RIO", "jungle_ally": "JgA",
              "jungle_enemy": "JgI", "base_ally": "BsA", "base_enemy": "BsI", None: " - "}
DRAGON_PT = {"FIRE_DRAGON": "infernal", "EARTH_DRAGON": "montanha", "WATER_DRAGON": "oceano", "AIR_DRAGON": "nuvem",
             "HEXTECH_DRAGON": "hextech", "CHEMTECH_DRAGON": "quimtec"}
MONSTER_PT = {"DRAGON": "dragão", "RIFTHERALD": "arauto", "BARON_NASHOR": "barão", "HORDE": "vastilarvas",
              "ATAKHAN": "atakhan", "ELDER_DRAGON": "dragão ancião"}

LIMITATIONS_BASE = [
    "Posições dos jogadores só existem a cada ~60s (um frame por minuto); o que acontece entre frames não é observado.",
    "Ganks/rotações que não terminaram em kill, assistência ou objetivo não geram evento: não dá para provar que um gank falhou ou não aconteceu.",
    "Flash e demais feitiços de invocador, uso de habilidades, dano trocado em lane e HP/mana não aparecem na timeline.",
    "Pings, chat, calls e voz não estão nos dados; nunca inferir intenção ou comunicação.",
    "'Quem estava perto' de objetivos/kills é ESTIMADO por interpolação linear entre frames de 60s (exceto quem matou/assistiu, que é certo).",
    "Recall não é um evento: é INFERIDO por gasto de ouro / posição na base sem morte recente.",
    "Roles podem ser inferidas (veja participants[].roleSource); confira roleConfidence.",
]
LIMITATIONS_LCU = [
    "Fonte LCU: a timeline do cliente só traz CHAMPION_KILL, ELITE_MONSTER_KILL e BUILDING_KILL. "
    "Não há wards colocadas/destruídas por evento, compras de itens, level-ups nem placas de torre. "
    "Wards só existem como totais por jogador (stats); compras são estimadas por ouro gasto por minuto. "
    "Para eventos completos, baixe a Match-V5 (RIOT_API_KEY + tools/fetch.py fetch <id> --source riot).",
    "Fonte LCU: a role (lane) não vem confiável do cliente; é inferida por Smite (jungle) e posição de 2-10 min.",
]
LIMITATIONS_V5 = [
    "Fonte Match-V5: eventos de ward não têm posição (só quem colocou/destruiu e o tipo).",
]


# ============================================================================ util
def champ_map() -> dict[int, str]:
    p = STATIC / "champions.json"
    return {int(c["id"]): c["name"] for c in load_json(p)} if p.exists() else {}


def item_map() -> dict[int, str]:
    p = STATIC / "items.json"
    return {int(i["id"]): i["name"] for i in load_json(p)} if p.exists() else {}


# ============================================================================ normalização
def load_game(gid: int) -> dict:
    """Carrega match + timeline (Match-V5 se houver, senão LCU) num formato comum."""
    d = MATCHES / str(gid)
    if (d / "timeline_v5.json").exists() and (d / "match_v5.json").exists():
        return _norm_v5(load_json(d / "match_v5.json"), load_json(d / "timeline_v5.json"))
    if (d / "timeline.json").exists() and (d / "match.json").exists():
        return _norm_lcu(load_json(d / "match.json"), load_json(d / "timeline.json"))
    raise SystemExit(f"Partida {gid} não está no cache. Rode: python tools/fetch.py fetch {gid}")


def _frames(raw_frames) -> list[dict]:
    out = []
    for f in raw_frames:
        pf = {}
        for k, v in f["participantFrames"].items():
            pos = v.get("position") or {}
            pf[int(k)] = {
                "x": pos.get("x"), "y": pos.get("y"), "totalGold": v.get("totalGold", 0),
                "currentGold": v.get("currentGold", 0), "xp": v.get("xp", 0), "level": v.get("level", 0),
                "cs": v.get("minionsKilled", 0) + v.get("jungleMinionsKilled", 0),
                "jungleCs": v.get("jungleMinionsKilled", 0),
            }
            cs_ = v.get("championStats") or {}  # só Match-V5: vida e mana no instante do frame
            if cs_.get("healthMax"):
                pf[int(k)]["hpPct"] = round(100 * cs_.get("health", 0) / cs_["healthMax"])
            if cs_.get("powerMax"):
                pf[int(k)]["manaPct"] = round(100 * cs_.get("power", 0) / cs_["powerMax"])
        out.append({"ts": f["timestamp"], "pf": pf, "events": f.get("events", [])})
    return out


def _norm_lcu(m: dict, t: dict) -> dict:
    champs = champ_map()
    ids = {i["participantId"]: i.get("player", {}) for i in m.get("participantIdentities", [])}
    parts = []
    for p in m["participants"]:
        s, pl = p.get("stats", {}), ids.get(p["participantId"], {})
        parts.append({
            "pid": p["participantId"], "team": p["teamId"], "championId": p["championId"],
            "champion": champs.get(p["championId"], str(p["championId"])),
            "riotId": f'{pl.get("gameName", "?")}#{pl.get("tagLine", "?")}', "puuid": pl.get("puuid"),
            "spells": [p.get("spell1Id"), p.get("spell2Id")], "position": None,
            "stats": {
                "kills": s.get("kills", 0), "deaths": s.get("deaths", 0), "assists": s.get("assists", 0),
                "cs": s.get("totalMinionsKilled", 0) + s.get("neutralMinionsKilled", 0),
                "gold": s.get("goldEarned", 0), "damageToChampions": s.get("totalDamageDealtToChampions", 0),
                "visionScore": s.get("visionScore"), "wardsPlaced": s.get("wardsPlaced"),
                "wardsKilled": s.get("wardsKilled"), "controlWardsBought": s.get("visionWardsBoughtInGame"),
                "champLevel": s.get("champLevel"), "win": bool(s.get("win")),
            },
        })
    win_team = next((tm["teamId"] for tm in m.get("teams", []) if tm.get("win") == "Win"), None)
    return {
        "gameId": m["gameId"], "source": "lcu", "platformId": m.get("platformId"), "queueId": m.get("queueId"),
        "mapId": m.get("mapId"), "gameCreation": m.get("gameCreation"), "duration": m.get("gameDuration"),
        "gameVersion": m.get("gameVersion"), "winnerTeam": win_team, "participants": parts,
        "frames": _frames(t["frames"]),
    }


def _norm_v5(m: dict, t: dict) -> dict:
    info = m["info"]
    champs = champ_map()  # nomes em pt-BR do cliente; a API usa nomes internos ("Chogath", "MonkeyKing")
    parts = []
    for p in info["participants"]:
        parts.append({
            "pid": p["participantId"], "team": p["teamId"], "champion": champs.get(p.get("championId")) or p.get("championName"),
            "championId": p.get("championId"),
            "riotId": f'{p.get("riotIdGameName", "?")}#{p.get("riotIdTagline", "?")}', "puuid": p.get("puuid"),
            "spells": [p.get("summoner1Id"), p.get("summoner2Id")], "position": p.get("teamPosition") or None,
            "stats": {
                "kills": p.get("kills", 0), "deaths": p.get("deaths", 0), "assists": p.get("assists", 0),
                "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
                "gold": p.get("goldEarned", 0), "damageToChampions": p.get("totalDamageDealtToChampions", 0),
                "visionScore": p.get("visionScore"), "wardsPlaced": p.get("wardsPlaced"),
                "wardsKilled": p.get("wardsKilled"), "controlWardsBought": p.get("visionWardsBoughtInGame"),
                "champLevel": p.get("champLevel"), "win": bool(p.get("win")),
            },
        })
    win_team = next((tm["teamId"] for tm in info.get("teams", []) if tm.get("win")), None)
    return {
        "gameId": info["gameId"], "source": "riot-match-v5", "platformId": info.get("platformId"),
        "queueId": info.get("queueId"), "mapId": info.get("mapId"), "gameCreation": info.get("gameCreation"),
        "duration": info.get("gameDuration"), "gameVersion": info.get("gameVersion"), "winnerTeam": win_team,
        "participants": parts, "frames": _frames(t["info"]["frames"]),
    }


# ============================================================================ roles
def infer_roles(g: dict) -> None:
    parts, frames, sr = g["participants"], g["frames"], g["mapId"] == 11
    if all(p["position"] for p in parts):
        for p in parts:
            p["role"], p["roleSource"], p["roleConfidence"] = p["position"], "match-v5 teamPosition", 1.0
        return
    early = frames[2:11]
    for team in (100, 200):
        tp = [p for p in parts if p["team"] == team]
        if not sr or len(tp) != 5:
            for p in tp:
                p["role"], p["roleSource"], p["roleConfidence"] = "", "indisponível (mapa/modo)", 0.0
            continue
        smite = [p for p in tp if 11 in p["spells"]]
        if len(smite) == 1:
            jg = smite[0]
        else:
            jg = max(tp, key=lambda p: frames[min(10, len(frames) - 1)]["pf"][p["pid"]]["jungleCs"])
        jg["role"], jg["roleSource"], jg["roleConfidence"] = "JUNGLE", "smite" if len(smite) == 1 else "cs de jungle", 1.0
        rest = [p for p in tp if p is not jg]
        cnt = {p["pid"]: Counter() for p in rest}
        for f in early:
            for p in rest:
                v = f["pf"][p["pid"]]
                if v["x"] is not None:
                    cnt[p["pid"]][zone_rel(v["x"], v["y"], team)] += 1
        best, best_score = None, -1
        for perm in permutations(["top", "mid", "bot", "bot"]):
            sc = sum(cnt[p["pid"]][z] for p, z in zip(rest, perm))
            if sc > best_score:
                best, best_score = perm, sc
        bots = []
        for p, z in zip(rest, best):
            n = sum(cnt[p["pid"]].values()) or 1
            p["roleConfidence"] = round(cnt[p["pid"]][z] / n, 2)
            p["roleSource"] = "inferida por posição 2-10min"
            if z == "bot":
                bots.append(p)
            else:
                p["role"] = "TOP" if z == "top" else "MIDDLE"
        f10 = frames[min(10, len(frames) - 1)]["pf"]
        bots.sort(key=lambda p: f10[p["pid"]]["cs"], reverse=True)
        bots[0]["role"], bots[1]["role"] = "BOTTOM", "UTILITY"
        for p in parts:
            p["position"] = p.get("position")


# ============================================================================ resumo
def summarize_game(gid: int) -> dict:
    g = load_game(gid)
    infer_roles(g)
    parts, frames = g["participants"], g["frames"]
    P = {p["pid"]: p for p in parts}
    sr = g["mapId"] == 11
    items = item_map()
    for p in parts:
        opp = [q for q in parts if q["team"] != p["team"] and q.get("role") and q["role"] == p.get("role")]
        p["laneOpponent"] = opp[0]["pid"] if len(opp) == 1 else None

    def label(pid):
        p = P.get(pid)
        return f'{p["champion"]}({pid})' if p else ("minion/torre" if pid == 0 else str(pid))

    # ---------------- posições interpoladas
    all_events = [e for f in frames for e in f["events"]]
    deaths_ts = defaultdict(list)
    for e in all_events:
        if e.get("type") == "CHAMPION_KILL":
            deaths_ts[e["victimId"]].append(e["timestamp"])
    positions = Positions(
        [(f["ts"], {pid: (v["x"], v["y"]) for pid, v in f["pf"].items()}) for f in frames],
        [(e["timestamp"], e["victimId"], e.get("killerId"), (e.get("position") or {}).get("x"),
          (e.get("position") or {}).get("y")) for e in all_events if e.get("type") == "CHAMPION_KILL"])

    def nearby(ts, x, y, radius, exclude=()):
        """Jogadores vivos a <= radius de (x,y) no instante ts (estimado; veja positions.py)."""
        out = []
        for pid in P:
            if pid in exclude:
                continue
            pp = positions.at(pid, ts - 1)  # instante imediatamente anterior ao evento
            if pp is None or pp["dead"]:
                continue
            dist = math.hypot(pp["x"] - x, pp["y"] - y)
            if dist <= radius:
                out.append({"pid": pid, "team": P[pid]["team"], "champion": P[pid]["champion"], "distEst": int(dist)})
        return sorted(out, key=lambda r: r["distEst"])

    # ---------------- minuto a minuto
    per_minute = []
    for fi, f in enumerate(frames):
        minute = fi  # frame i ~ minuto i; o último frame é o fim da partida
        players, teams = {}, {100: Counter(), 200: Counter()}
        for pid, v in f["pf"].items():
            team = P[pid]["team"]
            z = zone_rel(v["x"], v["y"], team) if (sr and v["x"] is not None) else None
            row = {"zone": z, "x": v["x"], "y": v["y"], "gold": v["totalGold"], "currentGold": v["currentGold"],
                   "xp": v["xp"], "cs": v["cs"], "level": v["level"],
                   "hpPct": v.get("hpPct"), "manaPct": v.get("manaPct")}
            players[pid] = row
            for k in ("gold", "xp", "cs"):
                teams[team][k] += row[k]
        for pid, row in players.items():
            o = P[pid]["laneOpponent"]
            if o in players:
                for k in ("gold", "xp", "cs"):
                    row[f"{k}Diff"] = row[k] - players[o][k]
        per_minute.append({
            "minute": minute, "ts": f["ts"], "time": mmss(f["ts"]), "isFinal": fi == len(frames) - 1 and fi > 0,
            "players": players,
            "teams": {str(t): dict(c) for t, c in teams.items()},
            "teamDiff100": {k: teams[100][k] - teams[200][k] for k in ("gold", "xp", "cs")},
        })

    # ---------------- eventos
    kills, objectives, wards, purchases = [], [], [], []
    for e in all_events:
        ty, ts = e.get("type"), e.get("timestamp", 0)
        pos = e.get("position") or {}
        x, y = pos.get("x"), pos.get("y")
        if ty == "CHAMPION_KILL":
            vic = e["victimId"]
            vteam = P[vic]["team"] if vic in P else None
            involved = [e.get("killerId", 0)] + list(e.get("assistingParticipantIds") or [])
            near = nearby(ts, x, y, NEAR_KILL, exclude=set(involved) | {vic}) if x is not None else []
            kills.append({
                "time": mmss(ts), "ts": ts, "minute": ts // 60000,
                "killer": e.get("killerId"), "killerLabel": label(e.get("killerId")),
                "victim": vic, "victimLabel": label(vic), "victimTeam": vteam,
                "assists": e.get("assistingParticipantIds") or [],
                "assistLabels": [label(a) for a in e.get("assistingParticipantIds") or []],
                "x": x, "y": y, "landmark": landmark(x, y) if sr else "",
                "zoneForVictim": zone_rel(x, y, vteam) if (sr and x is not None and vteam) else None,
                "alliesOfVictimNearEst": [n["pid"] for n in near if n["team"] == vteam],
                "otherEnemiesNearEst": [n["pid"] for n in near if n["team"] != vteam],
                "bounty": e.get("bounty"), "shutdownBounty": e.get("shutdownBounty"),
            })
        elif ty in ("ELITE_MONSTER_KILL", "BUILDING_KILL", "TURRET_PLATE_DESTROYED"):
            killer = e.get("killerId", 0)
            if ty == "ELITE_MONSTER_KILL":
                kteam = e.get("killerTeamId") or (P[killer]["team"] if killer in P else None)
                if kteam not in (100, 200):
                    kteam = None  # killerId 0 sem time: matador não identificado nos dados
                sub = e.get("monsterSubType") or ""
                name = MONSTER_PT.get(sub, MONSTER_PT.get(e.get("monsterType"), e.get("monsterType")))
                if e.get("monsterType") == "DRAGON" and sub and sub != "ELDER_DRAGON":
                    name = "dragão " + DRAGON_PT.get(sub, sub.replace("_DRAGON", "").lower())
                kind = e.get("monsterType")
            else:
                owner = e.get("teamId")
                kteam = 300 - owner if owner in (100, 200) else None
                lane = (e.get("laneType") or "").replace("_LANE", "").lower()
                if ty == "TURRET_PLATE_DESTROYED":
                    kind, name = "TURRET_PLATE", f"placa {lane}"
                elif e.get("buildingType") == "INHIBITOR_BUILDING":
                    kind, name = "INHIBITOR", f"inibidor {lane}"
                else:
                    kind = "TOWER"
                    name = f'torre {(e.get("towerType") or "").replace("_TURRET", "").lower()} {lane}'
                name += f" (do time {owner})"
            certain = [p for p in [killer] + list(e.get("assistingParticipantIds") or []) if p in P]
            near = nearby(ts, x, y, NEAR_OBJ, exclude=set(certain)) if x is not None else []
            objectives.append({
                "time": mmss(ts), "ts": ts, "minute": ts // 60000, "type": kind, "name": name,
                "killerTeam": kteam, "killer": killer, "killerLabel": label(killer),
                "participantsCertain": certain, "nearbyEst": near, "x": x, "y": y,
                "landmark": landmark(x, y) if sr else "",
            })
        elif ty in ("WARD_PLACED", "WARD_KILL"):
            pid = e.get("creatorId") if ty == "WARD_PLACED" else e.get("killerId")
            if pid in P:
                wards.append({"time": mmss(ts), "ts": ts, "type": ty, "pid": pid, "wardType": e.get("wardType")})
        elif ty in ("ITEM_PURCHASED", "ITEM_SOLD", "ITEM_UNDO"):
            pid = e.get("participantId")
            iid = e.get("itemId") or e.get("beforeId")
            purchases.append({"time": mmss(ts), "ts": ts, "type": ty, "pid": pid,
                              "itemId": iid, "item": items.get(iid, str(iid)), "estimated": False})

    has_item_events = any(p["type"] == "ITEM_PURCHASED" for p in purchases)
    if not has_item_events:  # LCU: estimar gastos por minuto a partir de totalGold - currentGold
        for i in range(1, len(frames)):
            a, b = frames[i - 1]["pf"], frames[i]["pf"]
            for pid in P:
                spent = (b[pid]["totalGold"] - b[pid]["currentGold"]) - (a[pid]["totalGold"] - a[pid]["currentGold"])
                if spent >= 100:
                    purchases.append({"time": f'{mmss(frames[i - 1]["ts"])}-{mmss(frames[i]["ts"])}',
                                      "ts": frames[i]["ts"], "type": "GOLD_SPENT_EST", "pid": pid,
                                      "goldSpent": spent, "estimated": True})

    # ---------------- recalls inferidos
    recalls = []
    for pid in P:
        team = P[pid]["team"]
        if has_item_events:
            buys = sorted(p["ts"] for p in purchases if p["pid"] == pid and p["type"] == "ITEM_PURCHASED" and p["ts"] > 90000)
            clusters = []
            for t0 in buys:
                if not clusters or t0 - clusters[-1][-1] > 30000:
                    clusters.append([t0])
                else:
                    clusters[-1].append(t0)
            visits = [c[0] for c in clusters]
            for t0 in visits:
                died = any(0 <= t0 - d <= 90000 for d in deaths_ts[pid])
                recalls.append({"pid": pid, "time": mmss(t0), "ts": t0,
                                "kind": "volta à base após morte" if died else "recall (inferido)",
                                "evidence": "compra de item"})
        else:
            for i in range(2, len(frames)):
                f0, f1 = frames[i - 1], frames[i]
                v = f1["pf"][pid]
                spent = (v["totalGold"] - v["currentGold"]) - (f0["pf"][pid]["totalGold"] - f0["pf"][pid]["currentGold"])
                in_base = sr and v["x"] is not None and zone_rel(v["x"], v["y"], team) == "base_ally"
                if spent < 100 and not in_base:
                    continue
                died = any(f0["ts"] - 60000 < d <= f1["ts"] for d in deaths_ts[pid])
                ev = []
                if spent >= 100:
                    ev.append(f"gastou ~{spent} de ouro entre {mmss(f0['ts'])} e {mmss(f1['ts'])}")
                if in_base:
                    ev.append(f"estava na base em {mmss(f1['ts'])}")
                recalls.append({"pid": pid, "time": f'{mmss(f0["ts"])}-{mmss(f1["ts"])}', "ts": f1["ts"],
                                "kind": "volta à base após morte" if died else "recall (inferido)",
                                "evidence": "; ".join(ev)})
        # recalls consecutivos (ex.: dois frames na base) contam uma vez
    recalls.sort(key=lambda r: (r["pid"], r["ts"]))
    dedup = []
    for r in recalls:
        if dedup and dedup[-1]["pid"] == r["pid"] and r["ts"] - dedup[-1]["ts"] <= 61000:
            continue
        dedup.append(r)
    recalls = sorted(dedup, key=lambda r: r["ts"])

    # ---------------- agregados por jogador
    def at(minute, pid, key):
        for row in per_minute:
            if row["minute"] == minute:
                return row["players"][pid].get(key)
        return None

    team_obj = {100: [o for o in objectives if o["killerTeam"] == 100 and o["type"] in ("DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN")],
                200: [o for o in objectives if o["killerTeam"] == 200 and o["type"] in ("DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN")]}
    team_kills = Counter(k["killer"] and P.get(k["killer"], {}).get("team") for k in kills)
    for p in parts:
        pid, team = p["pid"], p["team"]
        zones = Counter(row["players"][pid]["zone"] for row in per_minute[1:] if row["players"][pid]["zone"])
        tot = sum(zones.values()) or 1
        my_deaths = [k for k in kills if k["victim"] == pid]
        objs = team_obj[team]
        part_cert = sum(1 for o in objs if pid in o["participantsCertain"])
        part_est = sum(1 for o in objs if pid in o["participantsCertain"] or any(n["pid"] == pid for n in o["nearbyEst"]))
        kp = (p["stats"]["kills"] + p["stats"]["assists"]) / team_kills[team] if team_kills[team] else None
        p["aggregates"] = {
            "zoneShare": {z: round(c / tot, 3) for z, c in zones.most_common()},
            "deathMinutes": [k["minute"] for k in my_deaths],
            "deathZones": dict(Counter(k["zoneForVictim"] for k in my_deaths)),
            "deathsWithNoAllyNearEst": sum(1 for k in my_deaths if not k["alliesOfVictimNearEst"]),
            "cs10": at(10, pid, "cs"), "cs15": at(15, pid, "cs"),
            "gold10": at(10, pid, "gold"), "gold15": at(15, pid, "gold"),
            "goldDiff10": at(10, pid, "goldDiff"), "goldDiff15": at(15, pid, "goldDiff"),
            "csDiff10": at(10, pid, "csDiff"), "csDiff15": at(15, pid, "csDiff"),
            "xpDiff10": at(10, pid, "xpDiff"), "xpDiff15": at(15, pid, "xpDiff"),
            "teamEpicObjectives": len(objs), "objParticipationCertain": part_cert,
            "objParticipationIncludingNearEst": part_est,
            "killParticipation": round(kp, 3) if kp is not None else None,
            "recallsInferred": sum(1 for r in recalls if r["pid"] == pid and r["kind"].startswith("recall")),
        }

    limitations = list(LIMITATIONS_BASE) + (LIMITATIONS_LCU if g["source"] == "lcu" else LIMITATIONS_V5)
    if not sr:
        limitations.append(f"mapId {g['mapId']} não é Summoner's Rift: zonas não classificadas.")
    ward_summary = {
        "events": wards if wards else None,
        "totalsPerPlayer": {str(p["pid"]): {k: p["stats"][k] for k in ("wardsPlaced", "wardsKilled", "controlWardsBought", "visionScore")} for p in parts},
        "note": "eventos individuais disponíveis" if wards else "sem eventos de ward nesta fonte; só totais por jogador",
    }
    summary = {
        "summaryVersion": SUMMARY_VERSION, "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "gameId": g["gameId"], "source": g["source"], "platformId": g["platformId"],
        "queueId": g["queueId"], "queue": queue_name(g["queueId"]), "mapId": g["mapId"],
        "gameCreation": g["gameCreation"], "date": dt.datetime.fromtimestamp((g["gameCreation"] or 0) / 1000).isoformat(timespec="minutes"),
        "duration": g["duration"], "durationStr": mmss((g["duration"] or 0) * 1000), "gameVersion": g["gameVersion"],
        "winnerTeam": g["winnerTeam"],
        "zoneLegend": {k: v for k, v in ZONE_PT.items() if k in ZONE_SHORT},
        "participants": [{k: v for k, v in p.items() if k != "position"} for p in parts],
        "perMinute": per_minute,
        "events": {"kills": kills, "objectives": objectives, "wards": ward_summary,
                   "purchases": purchases, "recalls": recalls},
        "limitations": limitations,
    }
    save_json(SUMMARIES / f"{gid}.json", summary)
    return summary


# ============================================================================ impressão
def find_focus(s: dict, who: str | None) -> dict | None:
    return resolve_player(s["participants"], who) or (resolve_player(s["participants"], "me") if who is None else None)


def print_summary(s: dict, focus_who: str | None = None) -> None:
    ps = {p["pid"]: p for p in s["participants"]}
    focus = find_focus(s, focus_who)
    res = lambda t: "VITÓRIA" if s["winnerTeam"] == t else "derrota"
    print(f'=== Partida {s["gameId"]} | {s["queue"]} | {s["date"]} | {s["durationStr"]} | patch {s["gameVersion"]} | fonte: {s["source"]}')
    if focus:
        print(f'Foco: {focus["riotId"]} ({focus["champion"]}, time {focus["team"]}, {ROLE_PT.get(focus.get("role", ""), "?")}) -> {res(focus["team"])}')
    print()
    for team in (100, 200):
        print(f'Time {team} ({"azul" if team == 100 else "vermelho"}) - {res(team)}')
        for p in [q for q in s["participants"] if q["team"] == team]:
            st, ag = p["stats"], p["aggregates"]
            mark = "*" if focus and p["pid"] == focus["pid"] else " "
            role = ROLE_PT.get(p.get("role", ""), "?")
            conf = "" if (not p.get("role") or p.get("roleConfidence", 1) >= 0.6) else " (role incerta)"
            kda = f'{st["kills"]}/{st["deaths"]}/{st["assists"]}'
            print(f' {mark}{p["pid"]:>2} {role:<8} {p["champion"]:<13} {p["riotId"][:22]:<22} {kda:<9} CS {st["cs"]:<4} '
                  f'ouro {st["gold"]:<6} visão {st["visionScore"]} wards {st["wardsPlaced"]}/{st["wardsKilled"]}{conf}')
        print()

    if s["mapId"] == 11:
        print("Zonas por minuto (TOP/MID/BOT, RIO, JgA=jungle aliada, JgI=jungle inimiga, BsA/BsI=base aliada/inimiga; relativo ao time de cada jogador)")
        order = [p["pid"] for t in (100, 200) for p in sorted([q for q in s["participants"] if q["team"] == t],
                 key=lambda q: ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", ""].index(q.get("role", "")))]
        hdr = " min | " + " ".join(f'{ps[i]["champion"][:5]:>5}' for i in order[:5]) + " || " + \
              " ".join(f'{ps[i]["champion"][:5]:>5}' for i in order[5:]) + " | ouro azul-verm"
        print(hdr)
        for row in s["perMinute"]:
            z = [ZONE_SHORT.get(row["players"][i]["zone"], "?") for i in order]
            dead = {k["victim"] for k in s["events"]["kills"] if row["ts"] - 20000 <= k["ts"] <= row["ts"]}
            cells = [f'{(zz + ("†" if i in dead else "")):>5}' for zz, i in zip(z, order)]
            lab = "fim" if row.get("isFinal") else f'{row["minute"]:>3}'
            print(f' {lab:>3} | ' + " ".join(cells[:5]) + " || " + " ".join(cells[5:]) +
                  f' | {row["teamDiff100"]["gold"]:+6d}')
        print("(† = morreu nos 20s antes do frame)\n")

    print("Diferenças contra o oponente direto (ouro / CS / XP):" if s["mapId"] == 11 else "")
    for m in ((5, 10, 15, 20) if s["mapId"] == 11 else ()):
        row = next((r for r in s["perMinute"] if r["minute"] == m), None)
        if not row:
            continue
        cells = []
        for p in [q for q in s["participants"] if q["team"] == (focus["team"] if focus else 100)]:
            v = row["players"][p["pid"]]
            if "goldDiff" in v:
                cells.append(f'{ROLE_PT.get(p.get("role", ""), "?")}:{v["goldDiff"]:+d}/{v["csDiff"]:+d}/{v["xpDiff"]:+d}')
        print(f'  {m:>2} min  ' + "  ".join(cells))
    print()

    print("Objetivos:")
    for o in s["events"]["objectives"]:
        if o["type"] == "TURRET_PLATE":
            continue
        cert = ", ".join(ps[i]["champion"] for i in o["participantsCertain"]) or "—"
        near = ", ".join(f'{n["champion"]}~{n["distEst"]}' for n in o["nearbyEst"]) or "—"
        who = f'time {o["killerTeam"]} pegou' if o["killerTeam"] else "(matador não identificado)"
        print(f'  {o["time"]} {who} {o["name"]} | participaram: {cert} | perto(est.): {near}')
    print()

    print("Kills (vítima <- matador [+assist]; zona relativa à vítima; aliados da vítima perto, estimado):")
    for k in s["events"]["kills"]:
        mark = "*" if focus and focus["pid"] in [k["victim"], k["killer"], *k["assists"]] else " "
        ass = ("+" + ",".join(a.split("(")[0] for a in k["assistLabels"])) if k["assists"] else "solo"
        allies = ",".join(ps[a]["champion"] for a in k["alliesOfVictimNearEst"]) or "ninguém"
        print(f' {mark}{k["time"]} {k["victimLabel"]:<17} <- {k["killerLabel"]:<17} {ass:<34} '
              f'{ZONE_PT.get(k["zoneForVictim"], "-"):<14} {k.get("landmark", ""):<34} aliados perto: {allies}')
    print()

    if focus:
        ag = focus["aggregates"]
        print(f'Foco {focus["champion"]}: zonas {", ".join(f"{ZONE_PT.get(z, z)} {v:.0%}" for z, v in ag["zoneShare"].items())}')
        kp = "?" if ag["killParticipation"] is None else f'{ag["killParticipation"]:.0%}'
        print(f'  CS@10 {ag["cs10"]} CS@15 {ag["cs15"]} | dif. ouro vs oponente @10 {ag["goldDiff10"]} @15 {ag["goldDiff15"]} | '
              f'KP {kp} | objetivos épicos do time com participação certa {ag["objParticipationCertain"]}/{ag["teamEpicObjectives"]} '
              f'(incluindo proximidade estimada {ag["objParticipationIncludingNearEst"]})')
        rec = [r for r in s["events"]["recalls"] if r["pid"] == focus["pid"]]
        print("  Voltas à base: " + "; ".join(f'{r["time"]} {r["kind"]}' for r in rec))
        w = s["events"]["wards"]
        print(f'  Wards: {w["totalsPerPlayer"][str(focus["pid"])]} ({w["note"]})')
        if w["events"]:
            mine = [e for e in w["events"] if e["pid"] == focus["pid"]]
            print("  Eventos de ward: " + ", ".join(f'{e["time"]} {"colocou" if e["type"] == "WARD_PLACED" else "destruiu"} {e["wardType"]}' for e in mine))
        buys = [b for b in s["events"]["purchases"] if b["pid"] == focus["pid"]]
        if buys and not buys[0]["estimated"]:
            print("  Compras: " + ", ".join(f'{b["time"]} {b["item"]}' for b in buys if b["type"] == "ITEM_PURCHASED"))
    print()
    print("Limitações:")
    for l in s["limitations"]:
        print("  - " + l)


# ============================================================================ multi-partida
def cached_ids() -> list[int]:
    ids = []
    for d in MATCHES.iterdir() if MATCHES.exists() else []:
        if d.is_dir() and d.name.isdigit() and ((d / "timeline.json").exists() or (d / "timeline_v5.json").exists()):
            ids.append(int(d.name))
    return ids


_SUMMARY_CACHE: dict[int, dict] = {}


def get_summary(gid: int) -> dict:
    """Resumo da partida (gera se faltar ou estiver desatualizado). Fica em memória durante o processo:
    relatórios de várias partidas leem o mesmo resumo várias vezes. Trate o dict como somente leitura."""
    gid = int(gid)
    if gid in _SUMMARY_CACHE:
        return _SUMMARY_CACHE[gid]
    p = SUMMARIES / f"{gid}.json"
    s = None
    if p.exists():
        s = load_json(p)
        src_v5 = (MATCHES / str(gid) / "timeline_v5.json").exists()
        if not (s.get("summaryVersion") == SUMMARY_VERSION and (s["source"] != "lcu" or not src_v5)):
            s = None
    if s is None:
        summarize_game(gid)
        s = load_json(p)  # mesmo formato do disco (chaves str em perMinute[].players)
    _SUMMARY_CACHE[gid] = s
    return s


def aggregate_player(who: str, n: int, queue: int | None, result: str | None = None,
                     role: str | None = None, ranked: bool = False, since: str | None = None,
                     until: str | None = None) -> dict:
    rows = []
    role_key = {v: k for k, v in ROLE_PT.items()}.get(role, role.upper() if role else None)
    for gid in cached_ids():
        s = get_summary(gid)
        if queue is not None and s["queueId"] != queue:
            continue
        if ranked and s["queueId"] not in (420, 440):
            continue
        if (since and s["date"][:10] < since) or (until and s["date"][:10] > until):
            continue
        if s["mapId"] != 11 or (s["duration"] or 0) < 300:
            continue
        p = find_focus(s, who)
        if not p:
            continue
        if result and (p["stats"]["win"] != (result == "win")):
            continue
        if role_key and p.get("role") != role_key:
            continue
        rows.append((s, p))
    rows.sort(key=lambda r: r[0]["gameCreation"] or 0, reverse=True)
    rows = rows[:n]
    if not rows:
        raise SystemExit(f'Nenhuma partida em cache com "{who}". Rode: python tools/fetch.py fetch-many --n {n}')
    zone_t, death_zone, death_bucket = Counter(), Counter(), Counter()
    cs10, cs15, gd10, gd15, kp = [], [], [], [], []
    obj_c = obj_e = obj_t = deaths = wins = alone = 0
    per_game = []
    for s, p in rows:
        ag, st = p["aggregates"], p["stats"]
        mins = len(s["perMinute"]) - 1
        for z, share in ag["zoneShare"].items():
            zone_t[z] += share * mins
        for z, c in ag["deathZones"].items():
            death_zone[z] += c
        for m in ag["deathMinutes"]:
            death_bucket[f"{(m // 5) * 5:02d}-{(m // 5) * 5 + 5:02d}"] += 1
        for lst, k in ((cs10, "cs10"), (cs15, "cs15"), (gd10, "goldDiff10"), (gd15, "goldDiff15"), (kp, "killParticipation")):
            if ag.get(k) is not None:
                lst.append(ag[k])
        obj_c += ag["objParticipationCertain"]
        obj_e += ag["objParticipationIncludingNearEst"]
        obj_t += ag["teamEpicObjectives"]
        deaths += st["deaths"]
        wins += st["win"]
        my_deaths = [k for k in s["events"]["kills"] if k["victim"] == p["pid"]]
        alone += sum(1 for k in my_deaths if not k["alliesOfVictimNearEst"])
        per_game.append({"gameId": s["gameId"], "date": s["date"], "queue": s["queue"], "champion": p["champion"],
                         "role": p.get("role"), "win": st["win"], "kda": f'{st["kills"]}/{st["deaths"]}/{st["assists"]}',
                         "cs10": ag["cs10"], "goldDiff10": ag["goldDiff10"], "goldDiff15": ag["goldDiff15"],
                         "deathMinutes": ag["deathMinutes"], "source": s["source"]})
    avg = lambda l: round(sum(l) / len(l), 1) if l else None
    tz = sum(zone_t.values()) or 1
    total_min = sum(len(s["perMinute"]) - 1 for s, _ in rows) or 1
    return {
        "player": who, "games": len(rows), "wins": wins, "queueFilter": queue,
        "zoneShare": {z: round(v / tz, 3) for z, v in zone_t.most_common()},
        "deathsTotal": deaths, "deathsPerGame": round(deaths / len(rows), 2),
        "deathsPer10Min": round(10 * deaths / total_min, 2),
        "deathsByMinuteBucket": dict(sorted(death_bucket.items())),
        "deathsByZone": dict(death_zone.most_common()),
        "deathsWithNoAllyNearEst": alone,
        "cs10Avg": avg(cs10), "cs15Avg": avg(cs15), "goldDiff10Avg": avg(gd10), "goldDiff15Avg": avg(gd15),
        "killParticipationAvg": avg(kp),
        "objParticipationCertain": f"{obj_c}/{obj_t}", "objParticipationInclNearEst": f"{obj_e}/{obj_t}",
        "perGame": per_game,
        "limitations": LIMITATIONS_BASE,
    }


def print_aggregate(a: dict) -> None:
    print(f'=== Padrões de {a["player"]} em {a["games"]} partidas ({a["wins"]}V/{a["games"] - a["wins"]}D)'
          + (f' | fila {queue_name(a["queueFilter"])}' if a["queueFilter"] else ""))
    print("Tempo por zona: " + ", ".join(f'{ZONE_PT.get(z, z)} {v:.0%}' for z, v in a["zoneShare"].items()))
    print(f'Mortes: {a["deathsTotal"]} ({a["deathsPerGame"]}/partida, {a["deathsPer10Min"]}/10min); '
          f'sem aliado perto (est.): {a["deathsWithNoAllyNearEst"]}')
    print("Mortes por faixa de minuto: " + ", ".join(f"{k}: {v}" for k, v in a["deathsByMinuteBucket"].items()))
    print("Mortes por zona: " + ", ".join(f'{ZONE_PT.get(k, k)}: {v}' for k, v in a["deathsByZone"].items()))
    print(f'CS@10 médio {a["cs10Avg"]} | CS@15 médio {a["cs15Avg"]} | dif. ouro vs oponente @10 {a["goldDiff10Avg"]} @15 {a["goldDiff15Avg"]}')
    print(f'KP média {a["killParticipationAvg"]} | objetivos épicos do time: participação certa {a["objParticipationCertain"]}, '
          f'incluindo proximidade estimada {a["objParticipationInclNearEst"]}')
    print()
    print(f'{"gameId":<12} {"data":<17} {"fila":<16} {"campeão":<13} {"role":<8} {"res":<4} {"KDA":<9} {"CS10":>4} {"GD10":>6} {"GD15":>6}  mortes(min)')
    for g in a["perGame"]:
        gd10 = "" if g["goldDiff10"] is None else f'{g["goldDiff10"]:+d}'
        gd15 = "" if g["goldDiff15"] is None else f'{g["goldDiff15"]:+d}'
        print(f'{g["gameId"]:<12} {g["date"]:<17} {g["queue"][:16]:<16} {g["champion"][:13]:<13} {ROLE_PT.get(g["role"] or "", "?"):<8} '
              f'{"V" if g["win"] else "D":<4} {g["kda"]:<9} {g["cs10"] or "":>4} {gd10:>6} {gd15:>6}  {g["deathMinutes"]}')


def main() -> None:
    ap = argparse.ArgumentParser(description="Resumo analisável de partidas em cache.")
    ap.add_argument("gameIds", nargs="*", metavar="gameId",
                    help="um ou mais gameIds, 'latest' (mais recente em cache) ou '-' (lê ids da entrada padrão)")
    ap.add_argument("--focus", help='jogador em destaque ("Nome#TAG", nome ou campeão)')
    ap.add_argument("--player", help='modo multi-partida: "Nome#TAG"')
    ap.add_argument("--games", type=int, default=10)
    ap.add_argument("--queue", type=int, default=None)
    ap.add_argument("--ranked", action="store_true", help="modo --player: só Solo/Duo e Flex")
    ap.add_argument("--result", choices=["win", "loss"], help="modo --player: só vitórias ou só derrotas")
    ap.add_argument("--role", help="modo --player: top, jungle, mid, adc ou suporte")
    ap.add_argument("--since", help="modo --player: AAAA-MM-DD (inclusive)")
    ap.add_argument("--until", help="modo --player: AAAA-MM-DD (inclusive)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--all", action="store_true", help="(re)gera resumos de todas as partidas em cache")
    a = ap.parse_args()
    if a.all:
        for gid in cached_ids():
            summarize_game(gid)
            print(SUMMARIES / f"{gid}.json")
        return
    if a.player:
        agg = aggregate_player(a.player, a.games, a.queue, a.result, a.role, a.ranked, a.since, a.until)
        tag = a.player.replace("#", "_").replace(" ", "_")
        save_json(SUMMARIES / f"player-{tag}.json", agg, indent=1)
        print_aggregate(agg)
        print(f"\n(salvo em {SUMMARIES / f'player-{tag}.json'})")
        return
    ids = []
    for g in a.gameIds:
        if g == "-":
            ids += [ln.split()[0] for ln in sys.stdin if ln.strip() and not ln.startswith("#")]
        else:
            ids.append(g)
    if not ids:
        ap.error("informe gameId(s), '-' ou --player")
    for g in ids:
        gid = latest_cached() if g == "latest" else int(str(g).split("_")[-1])
        s = summarize_game(gid)
        if a.quiet:
            print(SUMMARIES / f"{gid}.json")  # caminho no stdout, para encadear
        else:
            print_summary(s, a.focus)
            print(f"(resumo salvo em {SUMMARIES / f'{gid}.json'})", file=sys.stderr)


def latest_cached() -> int:
    ids = cached_ids()
    if not ids:
        raise SystemExit("Cache vazio. Rode: lol fetch latest")

    def created(i):
        d = MATCHES / str(i)
        if (d / "match.json").exists():
            return load_json(d / "match.json").get("gameCreation", 0)
        return load_json(d / "match_v5.json")["info"].get("gameCreation", 0)
    return max(ids, key=created)


if __name__ == "__main__":
    main()
