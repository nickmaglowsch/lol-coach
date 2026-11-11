"""Consultas genéricas e componíveis sobre os resumos (filosofia Unix).

Cada comando faz uma coisa, lê o cache (gera o resumo se faltar) e escreve TSV no stdout
(cabeçalho na 1ª linha; --no-header para tirar) ou JSON Lines com --json. Mensagens vão para o stderr.
Jogador (-p) aceita: me, pid (1-10), "Nome#TAG", nome, campeão (Cho'Gath == chogath).
Tempos aceitam "15:31", "15" (minuto) ou milissegundos.

  lol players <gid>                                    participantes, roles e KDA
  lol find [--enemy C]... [--ally C]... [--champ C]... [--queue Q] [--win|--loss] [-p me]
                                                       partidas em cache que batem com os filtros
  lol frames <gid> [-p P]... [--from M] [--to M]       posição/zona/ouro/CS/XP por minuto
  lol events <gid> [--type kill,objective,recall,purchase,ward] [-p P]... [--from T] [--to T]
  lol at <gid> <T> [-p P]... [--ref X,Y]               posição interpolada de cada jogador no instante T
  lol series <gid> <métrica> [-p P]                    série por minuto: team|gold|goldDiff|cs|csDiff|xp|xpDiff|level|zone
  lol games [-p me] [--queue Q] [--n N] [--ranked]      uma linha por partida: resultado, mortes, diffs, ouro do time, objetivos
  lol lanes [-p me] [--queue Q] [--n N] [--ranked]      diff de ouro/CS de cada role do seu time contra o oponente direto
  lol paths [-p me] [--from M] [--to M] [--ranked]      lado do mapa em cada minuto do início (lado do dragão × lado do barão) e o que veio depois
  lol timers                                            horários dos objetivos inferidos do seu histórico (nasce até / renasce em)
  lol fights <gid> [--gap 20] [--min 3]                 lutas: abates em sequência (intervalo <= gap s), placar, local, objetivos em volta
  lol swings <gid> [--window 3] [--top 5]               maiores viradas de ouro do time em janelas de N minutos, com o que aconteceu
  lol decisions <gid> [-p me] [--kind lane,travessia,objetivo,ausente,recall,morte]   revisão de decisões com estado e resultado

Exemplos de composição:
  lol find --enemy Yorick --enemy Akali --queue 440 --no-header | cut -f1
  lol events <gameId> --type kill -p chogath --from 15:00 --to 16:00
  lol frames <gameId> -p chogath --to 15 --no-header | awk -F'\\t' '{print $1, $5}'
  lol series <gameId> team | sort -t$'\\t' -k2 -n | head -1        # pior minuto do time
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import sys

from common import SUMMARIES, mmss, parse_time, resolve_player
from landmarks import describe as landmark
from positions import from_summary
from summarize import ROLE_PT, cached_ids, get_summary
from zones import ZONE_PT, zone_rel


# ----------------------------------------------------------------------------- saída
class Out:
    def __init__(self, a):
        self.json, self.header = a.json, not a.no_header
        self._printed = False

    def row(self, d: dict) -> None:
        if self.json:
            print(json.dumps(d, ensure_ascii=False))
            return
        if self.header and not self._printed:
            print("\t".join(d.keys()))
        self._printed = True
        print("\t".join("" if v is None else (",".join(map(str, v)) if isinstance(v, list) else str(v)) for v in d.values()))


def players_of(s: dict, sels: list[str] | None) -> list[dict]:
    if not sels:
        return s["participants"]
    out = []
    for sel in sels:
        p = resolve_player(s["participants"], sel)
        if p is None:
            raise SystemExit(f'Jogador "{sel}" não encontrado na partida {s["gameId"]}. Use: lol players {s["gameId"]}')
        out.append(p)
    return out


def load(gid) -> dict:
    return get_summary(int(str(gid).split("_")[-1]))


# ----------------------------------------------------------------------------- comandos
def cmd_players(a, out: Out) -> None:
    s = load(a.gid)
    for p in s["participants"]:
        st = p["stats"]
        out.row({"pid": p["pid"], "team": p["team"], "role": ROLE_PT.get(p.get("role", ""), "?"),
                 "roleConf": p.get("roleConfidence"), "champion": p["champion"], "riotId": p["riotId"],
                 "kda": f'{st["kills"]}/{st["deaths"]}/{st["assists"]}', "cs": st["cs"], "gold": st["gold"],
                 "win": st["win"], "laneOpponent": p.get("laneOpponent")})


def cmd_find(a, out: Out) -> None:
    for gid in sorted(cached_ids(), reverse=True):
        s = get_summary(gid)
        if a.queue is not None and s["queueId"] != a.queue:
            continue
        me = resolve_player(s["participants"], a.player)
        if me is None:
            continue
        allies = [p for p in s["participants"] if p["team"] == me["team"]]
        enemies = [p for p in s["participants"] if p["team"] != me["team"]]
        has = lambda grp, c: resolve_player(grp, c) is not None
        if any(not has(enemies, c) for c in a.enemy) or any(not has(allies, c) for c in a.ally) \
                or any(not has(s["participants"], c) for c in a.champ):
            continue
        if (a.win and not me["stats"]["win"]) or (a.loss and me["stats"]["win"]):
            continue
        out.row({"gameId": s["gameId"], "date": s["date"], "queue": s["queue"], "champion": me["champion"],
                 "role": ROLE_PT.get(me.get("role", ""), "?"), "win": me["stats"]["win"],
                 "kda": f'{me["stats"]["kills"]}/{me["stats"]["deaths"]}/{me["stats"]["assists"]}',
                 "allies": [p["champion"] for p in allies if p is not me], "enemies": [p["champion"] for p in enemies]})


def cmd_frames(a, out: Out) -> None:
    s = load(a.gid)
    ps = players_of(s, a.player)
    lo, hi = a.from_, a.to if a.to is not None else 10 ** 6
    for r in s["perMinute"]:
        if not (lo <= r["minute"] <= hi):
            continue
        for p in ps:
            v = r["players"][str(p["pid"])]
            out.row({"minute": r["minute"], "time": r["time"], "champion": p["champion"], "team": p["team"],
                     "zone": v["zone"], "x": v["x"], "y": v["y"], "gold": v["gold"], "cs": v["cs"], "xp": v["xp"],
                     "level": v["level"], "goldDiff": v.get("goldDiff"), "csDiff": v.get("csDiff"),
                     "xpDiff": v.get("xpDiff"), "landmark": landmark(v["x"], v["y"]) if s["mapId"] == 11 else ""})


def _in_window(ts, a) -> bool:
    lo = parse_time(a.from_) if a.from_ is not None else 0
    hi = parse_time(a.to) if a.to is not None else 10 ** 9
    return lo <= ts <= hi


def cmd_events(a, out: Out) -> None:
    s = load(a.gid)
    P = {p["pid"]: p for p in s["participants"]}
    pids = {p["pid"] for p in players_of(s, a.player)} if a.player else None
    types = set(a.type.split(",")) if a.type else {"kill", "objective"}
    rows = []
    ev = s["events"]
    if "kill" in types:
        for k in ev["kills"]:
            inv = {k["killer"], k["victim"], *k["assists"]}
            if pids and not (inv & pids):
                continue
            rows.append((k["ts"], {"time": k["time"], "type": "kill",
                                   "who": P[k["victim"]]["champion"] if k["victim"] in P else k["victim"],
                                   "team": k["victimTeam"], "by": k["killerLabel"].split("(")[0],
                                   "with": [P[x]["champion"] for x in k["assists"]],
                                   "zone": ZONE_PT.get(k["zoneForVictim"], k["zoneForVictim"]),
                                   "landmark": k.get("landmark", ""), "x": k["x"], "y": k["y"],
                                   "nearEst": [P[x]["champion"] for x in k["alliesOfVictimNearEst"]]}))
    if "objective" in types:
        for o in ev["objectives"]:
            if pids and not ((set(o["participantsCertain"]) | {n["pid"] for n in o["nearbyEst"]}) & pids):
                continue
            rows.append((o["ts"], {"time": o["time"], "type": "objective", "who": o["name"], "team": o["killerTeam"],
                                   "by": o["killerLabel"].split("(")[0],
                                   "with": [P[x]["champion"] for x in o["participantsCertain"] if x != o["killer"]],
                                   "zone": "", "landmark": o.get("landmark", ""), "x": o["x"], "y": o["y"],
                                   "nearEst": [n["champion"] for n in o["nearbyEst"]]}))
    if "recall" in types:
        for r in ev["recalls"]:
            if pids and r["pid"] not in pids:
                continue
            rows.append((r["ts"], {"time": r["time"], "type": "recall", "who": P[r["pid"]]["champion"],
                                   "team": P[r["pid"]]["team"], "by": "", "with": [], "zone": r["kind"],
                                   "landmark": r["evidence"], "x": None, "y": None, "nearEst": []}))
    if "purchase" in types:
        for b in ev["purchases"]:
            if pids and b["pid"] not in pids:
                continue
            what = b.get("item") or f'~{b.get("goldSpent")} de ouro (estimado)'
            rows.append((b["ts"], {"time": b["time"], "type": b["type"].lower(), "who": P[b["pid"]]["champion"],
                                   "team": P[b["pid"]]["team"], "by": "", "with": [], "zone": "", "landmark": what,
                                   "x": None, "y": None, "nearEst": []}))
    if "ward" in types:
        for w in (ev["wards"]["events"] or []):
            if pids and w["pid"] not in pids:
                continue
            rows.append((w["ts"], {"time": w["time"], "type": w["type"].lower(), "who": P[w["pid"]]["champion"],
                                   "team": P[w["pid"]]["team"], "by": "", "with": [], "zone": "",
                                   "landmark": w["wardType"], "x": None, "y": None, "nearEst": []}))
        if not ev["wards"]["events"]:
            print("[info] sem eventos de ward nesta fonte (só totais: lol players / summary)", file=sys.stderr)
    for ts, row in sorted(rows, key=lambda r: r[0]):
        if _in_window(ts, a):
            out.row(row)


def cmd_at(a, out: Out) -> None:
    s = load(a.gid)
    ts = parse_time(a.time)
    pos = from_summary(s)
    ref = tuple(float(v) for v in a.ref.split(",")) if a.ref else None
    for p in players_of(s, a.player):
        e = pos.at(p["pid"], ts)
        xy = (int(e["x"]), int(e["y"])) if e else None
        row = {"time": mmss(ts), "champion": p["champion"], "team": p["team"],
               "x": xy[0] if xy else None, "y": xy[1] if xy else None,
               "zone": ZONE_PT.get(zone_rel(*xy, p["team"])) if (xy and s["mapId"] == 11) else "",
               "landmark": landmark(*xy) if (xy and s["mapId"] == 11) else "",
               "state": ("morto desde " + mmss(e["deathTs"])) if (e and e["dead"]) else (e["source"] if e else "")}
        if ref:
            row["distRef"] = int(math.hypot(xy[0] - ref[0], xy[1] - ref[1])) if xy else None
        out.row(row)
    print("[info] state: frame = posição registrada; kill/death = local de evento; interp = estimativa entre âncoras",
          file=sys.stderr)


def cmd_series(a, out: Out) -> None:
    s = load(a.gid)
    p = resolve_player(s["participants"], a.player[0] if a.player else "me")
    if p is None and a.metric != "team":
        raise SystemExit("Informe -p <jogador>.")
    sign = -1 if (p and p["team"] == 200) else 1
    for r in s["perMinute"]:
        if a.metric == "team":
            v = sign * r["teamDiff100"]["gold"]
        else:
            v = r["players"][str(p["pid"])].get(a.metric)
        out.row({"minute": r["minute"], a.metric: v})


RANKED = {420, 440}
EPIC_COUNT = {"DRAGON": "drag", "BARON_NASHOR": "baron", "RIFTHERALD": "herald", "HORDE": "grubs"}


def my_games(a) -> list[tuple[dict, dict]]:
    """(resumo, jogador) das partidas em cache do jogador, mais recentes primeiro (só Summoner's Rift, sem remakes)."""
    rows = []
    for gid in cached_ids():
        s = get_summary(gid)
        if s["mapId"] != 11 or (s["duration"] or 0) < 300:
            continue
        if a.queue is not None and s["queueId"] != a.queue:
            continue
        if a.ranked and s["queueId"] not in RANKED:
            continue
        if getattr(a, "since", None) and s["date"][:10] < a.since:
            continue
        if getattr(a, "until", None) and s["date"][:10] > a.until:
            continue
        p = resolve_player(s["participants"], a.player)
        if p:
            rows.append((s, p))
    rows.sort(key=lambda r: r[0]["gameCreation"] or 0, reverse=True)
    return rows[:a.n] if a.n else rows


def _team_series(s: dict, team: int) -> list[int]:
    sign = -1 if team == 200 else 1
    return [sign * r["teamDiff100"]["gold"] for r in s["perMinute"]]


def cmd_games(a, out: Out) -> None:
    for s, p in my_games(a):
        team, ag, st = p["team"], p["aggregates"], p["stats"]
        ts = _team_series(s, team)
        at = lambda m: ts[min(m, len(ts) - 1)] if len(ts) > m else None
        imax = max(range(len(ts)), key=lambda i: ts[i]); imin = min(range(len(ts)), key=lambda i: ts[i])
        obj = {}
        for o in s["events"]["objectives"]:
            k = EPIC_COUNT.get(o["type"])
            if not k or o["killerTeam"] not in (100, 200):
                continue
            if o["type"] == "DRAGON" and "ancião" in o["name"]:
                k = "elder"
            side = "a" if o["killerTeam"] == team else "e"
            obj[(k, side)] = obj.get((k, side), 0) + 1
        towers = {sd: sum(1 for o in s["events"]["objectives"] if o["type"] == "TOWER" and o["killerTeam"] in (100, 200)
                          and (("a" if o["killerTeam"] == team else "e") == sd)) for sd in ("a", "e")}
        my_deaths = [k for k in s["events"]["kills"] if k["victim"] == p["pid"]]
        out.row({"gameId": s["gameId"], "date": s["date"][:16], "queue": s["queue"], "champion": p["champion"],
                 "role": ROLE_PT.get(p.get("role", ""), "?"), "win": st["win"], "min": round(s["duration"] / 60),
                 "kda": f'{st["kills"]}/{st["deaths"]}/{st["assists"]}', "deaths": st["deaths"],
                 "deathsTo15": sum(1 for k in my_deaths if k["ts"] < 900000),
                 "deathsAloneEst": sum(1 for k in my_deaths if not k["alliesOfVictimNearEst"]),
                 "deathsEnemyHalf": sum(1 for k in my_deaths if k["zoneForVictim"] in ("jungle_enemy", "base_enemy")),
                 "cs10": ag["cs10"], "gd10": ag["goldDiff10"], "gd15": ag["goldDiff15"],
                 "kp": ag["killParticipation"],
                 "team10": at(10), "team15": at(15), "team20": at(20),
                 "teamMax": ts[imax], "teamMaxMin": imax, "teamMin": ts[imin], "teamMinMin": imin,
                 "drag": f'{obj.get(("drag", "a"), 0)}-{obj.get(("drag", "e"), 0)}',
                 "elder": f'{obj.get(("elder", "a"), 0)}-{obj.get(("elder", "e"), 0)}',
                 "baron": f'{obj.get(("baron", "a"), 0)}-{obj.get(("baron", "e"), 0)}',
                 "grubs": f'{obj.get(("grubs", "a"), 0)}-{obj.get(("grubs", "e"), 0)}',
                 "herald": f'{obj.get(("herald", "a"), 0)}-{obj.get(("herald", "e"), 0)}',
                 "towers": f'{towers["a"]}-{towers["e"]}',
                 "objPartCert": f'{ag["objParticipationCertain"]}/{ag["teamEpicObjectives"]}'})


def cmd_lanes(a, out: Out) -> None:
    for s, me in my_games(a):
        for p in [q for q in s["participants"] if q["team"] == me["team"]]:
            v10 = next((r["players"][str(p["pid"])] for r in s["perMinute"] if r["minute"] == 10), {})
            v15 = next((r["players"][str(p["pid"])] for r in s["perMinute"] if r["minute"] == 15), {})
            out.row({"gameId": s["gameId"], "win": me["stats"]["win"], "role": ROLE_PT.get(p.get("role", ""), "?"),
                     "roleConf": p.get("roleConfidence"), "player": p["riotId"].split("#")[0], "champion": p["champion"],
                     "me": p is me, "gd10": v10.get("goldDiff"), "gd15": v15.get("goldDiff"), "csd15": v15.get("csDiff"),
                     "deaths": p["stats"]["deaths"],
                     "deathsTo15": sum(1 for k in s["events"]["kills"] if k["victim"] == p["pid"] and k["ts"] < 900000)})


def cmd_paths(a, out: Out) -> None:
    """Lado do mapa do jogador nos minutos [from, to]: 'bot' = metade do dragão (y - x < -1200),
    'top' = metade do barão/vastilarvas (y - x > 1200), 'centro' = faixa da mid/rio central, 'base'."""
    from zones import zone_abs
    lo, hi = a.from_, a.to
    for s, p in my_games(a):
        cnt = Counter()
        for r in s["perMinute"]:
            if lo <= r["minute"] <= hi:
                v = r["players"][str(p["pid"])]
                if v["x"] is None:
                    continue
                z = zone_abs(v["x"], v["y"])
                cnt["base" if z.startswith("base") else "top" if v["y"] - v["x"] > 1200 else "bot" if v["y"] - v["x"] < -1200 else "centro"] += 1
        team = p["team"]
        drags = [o for o in s["events"]["objectives"] if o["type"] == "DRAGON" and o["killerTeam"] in (100, 200)]
        grubs = [o for o in s["events"]["objectives"] if o["type"] == "HORDE" and o["killerTeam"] in (100, 200)]
        side = "bot" if cnt["bot"] > cnt["top"] else "top" if cnt["top"] > cnt["bot"] else "empate"
        t15 = next((r["teamDiff100"]["gold"] * (-1 if team == 200 else 1) for r in s["perMinute"] if r["minute"] == 15), None)
        out.row({"gameId": s["gameId"], "win": p["stats"]["win"], "role": ROLE_PT.get(p.get("role", ""), "?"),
                 "champion": p["champion"], "bot": cnt["bot"], "top": cnt["top"], "centro": cnt["centro"], "base": cnt["base"],
                 "side": side,
                 "firstDragon": ("nosso " if drags and drags[0]["killerTeam"] == team else "inimigo ") + drags[0]["time"] if drags else "",
                 "dragTo20": f'{sum(1 for o in drags if o["ts"] < 1200000 and o["killerTeam"] == team)}-{sum(1 for o in drags if o["ts"] < 1200000 and o["killerTeam"] != team)}',
                 "grubs": f'{sum(1 for o in grubs if o["killerTeam"] == team)}-{sum(1 for o in grubs if o["killerTeam"] != team)}',
                 "gd15": p["aggregates"]["goldDiff15"], "team15": t15})


def all_summaries() -> list[dict]:
    return [get_summary(g) for g in cached_ids()]


def fights_of(s: dict, team: int, gap_s: int = 20, min_kills: int = 3) -> list[dict]:
    """Agrupa abates em lutas: sequência com intervalo <= gap_s entre abates seguidos."""
    from landmarks import describe
    P = {p["pid"]: p for p in s["participants"]}
    ks = sorted((k for k in s["events"]["kills"] if k["x"] is not None), key=lambda k: k["ts"])
    groups, cur = [], []
    for k in ks:
        if cur and k["ts"] - cur[-1]["ts"] > gap_s * 1000:
            groups.append(cur); cur = []
        cur.append(k)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        if len(g) < min_kills:
            continue
        t0, t1 = g[0]["ts"], g[-1]["ts"]
        ours = sum(1 for k in g if P.get(k["victim"], {}).get("team") not in (team, None))
        cx, cy = sum(k["x"] for k in g) / len(g), sum(k["y"] for k in g) / len(g)
        objs = [o for o in s["events"]["objectives"] if t0 - 30000 <= o["ts"] <= t1 + 60000 and o["killerTeam"] in (100, 200)
                and o["type"] != "TURRET_PLATE"]
        out.append({"from": mmss(t0), "to": mmss(t1), "kills": len(g), "ours": ours, "theirs": len(g) - ours,
                    "where": describe(cx, cy) if s["mapId"] == 11 else "",
                    "zone": zone_rel(cx, cy, team) if s["mapId"] == 11 else "",
                    "objectives": [("+" if o["killerTeam"] == team else "-") + o["name"] for o in objs],
                    "fromMs": t0, "toMs": t1})
    return out


def swings_of(s: dict, team: int, window: int = 3, top: int = 5) -> list[dict]:
    """Maiores variações do ouro do time em janelas de `window` minutos (sem sobrepor)."""
    pm = s["perMinute"]
    sign = -1 if team == 200 else 1
    g = [sign * r["teamDiff100"]["gold"] for r in pm]
    cand = sorted(((g[i + window] - g[i], i) for i in range(len(g) - window)), key=lambda t: -abs(t[0]))
    chosen = []
    for d, i in cand:
        if any(abs(i - j) < window for _, j in chosen):
            continue
        chosen.append((d, i))
        if len(chosen) >= top:
            break
    P = {p["pid"]: p for p in s["participants"]}
    out = []
    for d, i in sorted(chosen, key=lambda t: t[1]):
        t0, t1 = pm[i]["ts"], pm[i + window]["ts"]
        ks = [k for k in s["events"]["kills"] if t0 <= k["ts"] <= t1]
        ours = sum(1 for k in ks if P.get(k["victim"], {}).get("team") not in (team, None))
        objs = [("+" if o["killerTeam"] == team else "-") + o["name"] for o in s["events"]["objectives"]
                if t0 <= o["ts"] <= t1 and o["killerTeam"] in (100, 200) and o["type"] != "TURRET_PLATE"]
        out.append({"from": pm[i]["time"], "to": pm[i + window]["time"], "goldFrom": g[i], "goldTo": g[i + window],
                    "delta": d, "kills": f"{ours}x{len(ks) - ours}", "objectives": objs,
                    "fromMin": i, "toMin": i + window})
    return out


def cmd_fights(a, out: Out) -> None:
    s = load(a.gid)
    p = resolve_player(s["participants"], a.player[0] if a.player else "me") or s["participants"][0]
    for f in fights_of(s, p["team"], a.gap, a.min):
        f.pop("fromMs"); f.pop("toMs")
        out.row(f)


def cmd_swings(a, out: Out) -> None:
    s = load(a.gid)
    p = resolve_player(s["participants"], a.player[0] if a.player else "me") or s["participants"][0]
    for w in swings_of(s, p["team"], a.window, a.top):
        w.pop("fromMin"); w.pop("toMin")
        out.row(w)


def cmd_timers(a, out: Out) -> None:
    from decisions import infer_timers
    for k, v in sorted(infer_timers(all_summaries()).items(), key=lambda kv: kv[1]["first"]):
        out.row({"objetivo": k, "primeiroAbateMaisCedo": mmss(v["first"]),
                 "menorIntervaloEntreAbates": mmss(v["respawn"]) if v["respawn"] else "", "partidas": v["games"]})


def cmd_decisions(a, out: Out) -> None:
    from decisions import infer_timers, review
    s = load(a.gid)
    p = resolve_player(s["participants"], a.player[0] if a.player else "me")
    if not p:
        raise SystemExit("Jogador não encontrado; use -p.")
    P = {q["pid"]: q for q in s["participants"]}
    kinds = set(a.kind.split(",")) if a.kind else None
    for d in review(s, p, infer_timers(all_summaries()), lambda pid: P[pid]["champion"] if pid in P else "minion/torre"):
        if kinds and d["kind"] not in kinds:
            continue
        r = d["result"]
        if a.json:
            out.row(d)
            continue
        out.row({"time": d["time"], "kind": d["kind"], "what": d["what"],
                 "hp": d["state"].get("hp"), "bolso": d["state"].get("bolso"), "nivel": d["state"]["nivel"],
                 "lane(nível;vida)": f'{d["lane"]["nivel"]};{d["lane"]["vida"]}' if d["lane"] else "",
                 "jgInimigo": f'{d["enemyJungle"]["zona"]} ~{d["enemyJungle"]["dist"]}' if d["enemyJungle"] else "",
                 "vivos": d["alive"], "sinais": d["signals"], "janela": d["window"],
                 "abates": f'{r["abatesNossos"]}x{r["abatesDeles"]} (você em {r["comVoce"]})',
                 "objetivos": "+" + ",".join(r["objetivosNossos"]) + " -" + ",".join(r["objetivosDeles"]),
                 "ouroTime": r["ouroTime"],
                 "chance": f'{r["chance"][0]}%→{r["chance"][1]}%' if r.get("chance") else "",
                 "veredito": d["verdict"], "sinaisFracos": d.get("weakSignals", [])})


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON Lines em vez de TSV")
    common.add_argument("--no-header", action="store_true")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("players", parents=[common]); sp.add_argument("gid")
    sp = sub.add_parser("find", parents=[common])
    sp.add_argument("--enemy", action="append", default=[]); sp.add_argument("--ally", action="append", default=[])
    sp.add_argument("--champ", action="append", default=[]); sp.add_argument("--queue", type=int)
    sp.add_argument("-p", "--player", default="me"); sp.add_argument("--win", action="store_true")
    sp.add_argument("--loss", action="store_true")
    for name in ("games", "lanes", "paths"):
        sp = sub.add_parser(name, parents=[common])
        if name == "paths":
            sp.add_argument("--from", dest="from_", type=int, default=1); sp.add_argument("--to", type=int, default=10)
        sp.add_argument("-p", "--player", default="me"); sp.add_argument("--queue", type=int)
        sp.add_argument("--n", type=int, default=0); sp.add_argument("--ranked", action="store_true", help="só Solo/Duo e Flex")
        sp.add_argument("--since", help="AAAA-MM-DD (inclusive)"); sp.add_argument("--until", help="AAAA-MM-DD (inclusive)")
    sub.add_parser("timers", parents=[common])
    sp = sub.add_parser("fights", parents=[common]); sp.add_argument("gid"); sp.add_argument("-p", "--player", action="append")
    sp.add_argument("--gap", type=int, default=20); sp.add_argument("--min", type=int, default=3)
    sp = sub.add_parser("swings", parents=[common]); sp.add_argument("gid"); sp.add_argument("-p", "--player", action="append")
    sp.add_argument("--window", type=int, default=3); sp.add_argument("--top", type=int, default=5)
    sp = sub.add_parser("decisions", parents=[common]); sp.add_argument("gid"); sp.add_argument("-p", "--player", action="append")
    sp.add_argument("--kind", help="lane,travessia,objetivo,ausente,recall,morte")
    for name in ("frames", "events", "series", "at"):
        sp = sub.add_parser(name, parents=[common]); sp.add_argument("gid")
        sp.add_argument("-p", "--player", action="append")
        if name == "series":
            sp.add_argument("metric", choices=["team", "gold", "goldDiff", "cs", "csDiff", "xp", "xpDiff", "level", "zone"])
        if name == "at":
            sp.add_argument("time"); sp.add_argument("--ref", help="X,Y para medir distância")
        if name in ("frames", "events"):
            sp.add_argument("--from", dest="from_", default=0 if name == "frames" else None,
                            type=int if name == "frames" else str)
            sp.add_argument("--to", default=None, type=int if name == "frames" else str)
        if name == "events":
            sp.add_argument("--type", help="kill,objective,recall,purchase,ward (padrão: kill,objective)")
    a = ap.parse_args()
    out = Out(a)
    try:
        {"players": cmd_players, "find": cmd_find, "frames": cmd_frames, "events": cmd_events,
         "at": cmd_at, "series": cmd_series, "games": cmd_games, "lanes": cmd_lanes, "paths": cmd_paths,
         "timers": cmd_timers, "decisions": cmd_decisions, "fights": cmd_fights, "swings": cmd_swings}[a.cmd](a, out)
    except BrokenPipeError:  # ex.: | head
        sys.stderr.close()


if __name__ == "__main__":
    main()
