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

Exemplos de composição:
  lol find --enemy Yorick --enemy Akali --queue 440 --no-header | cut -f1
  lol events <gameId> --type kill -p chogath --from 15:00 --to 16:00
  lol frames <gameId> -p chogath --to 15 --no-header | awk -F'\\t' '{print $1, $5}'
  lol series <gameId> team | sort -t$'\\t' -k2 -n | head -1        # pior minuto do time
"""
from __future__ import annotations

import argparse
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
         "at": cmd_at, "series": cmd_series}[a.cmd](a, out)
    except BrokenPipeError:  # ex.: | head
        sys.stderr.close()


if __name__ == "__main__":
    main()
