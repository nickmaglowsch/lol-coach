"""Chance de vitória por minuto (regressão logística) e padrões de jungle por campeão.

Uso:
  python tools/winprob.py crawl --max 1000 [--queue 420,440] [--per-player 10] [--depth 1]
  python tools/winprob.py train [--holdout 0.2] [--l2 1.0] [--queue 420,440]
  python tools/winprob.py curve <gameId> [--team me|100|200] [--step 60]
  python tools/winprob.py state <gameId> <T> [--team me|100|200]
  python tools/winprob.py junglers [--champ Shaco] [--min-games 5] [--rebuild]

Dados (data/winprob/, fora do git):
  samples.jsonl          uma linha por partida: gameId, fila, patch, campeões por role, totais por time
                         a cada frame (ouro, XP, CS, nível), eventos compactos (abates, torres,
                         inibidores, placas, dragões, ancião, arauto, vastilarvas, barão, atakhan) e o
                         começo de jogo dos dois junglers. SEM nomes, Riot IDs ou PUUIDs.
  seen.txt               gameIds já processados (o crawl retoma daqui).
  model.json             pesos do modelo + métricas do teste.
  jungle_patterns.json   padrões dos junglers por campeão (minutos 1-5, primeira participação em abate).

Importável (para a revisão de decisões):
  from winprob import state_at, win_prob, win_curve, win_prob_at
  st = state_at(summary_ou_gameId, "15:31")   # estado do ponto de vista do time AZUL (100)
  p  = win_prob(st, team=200)                 # chance de vitória do time 200 nesse estado

Features que o resumo do projeto (data/summaries/<id>.json) fornece para state_at:
  - ouro, XP e CS por time: perMinute[].teams (só nos frames, ~1/min; entre frames é interpolado);
  - nível médio: perMinute[].players[*].level;
  - abates: events.kills[].victimTeam (instante exato);
  - torres, inibidores, placas, dragões (ancião pelo nome "dragão ancião"), arauto, vastilarvas,
    barão, atakhan: events.objectives[] (type + killerTeam, instante exato);
  - alma de dragão: inferida (primeiro time a 4 dragões elementais);
  - buff de barão (180 s) e de ancião (150 s): janela fixa após o abate (não sabe quem morreu com o buff);
  - inibidor caído: janela de 300 s após a destruição.
  Resumos de fonte LCU não têm placas (TURRET_PLATE): a feature fica 0 nesses jogos.

Regras: só GET de leitura pela classe RiotAPI (chave nunca impressa); nada de nomes/PUUIDs gravados.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
import time
import zlib
from collections import Counter, defaultdict, deque

from common import DATA, MATCHES, STATIC, RiotAPI, RiotAPIError, load_json, mmss, parse_time, save_json
from zones import zone_abs, zone_rel

WP = DATA / "winprob"
SAMPLES = WP / "samples.jsonl"
SEEN = WP / "seen.txt"
MODEL = WP / "model.json"
JUNGLE = WP / "jungle_patterns.json"

SAMPLE_VERSION = 1
BARON_BUFF_MS = 180_000
ELDER_BUFF_MS = 150_000
INHIB_RESPAWN_MS = 300_000
JG_FRAMES = 9            # minutos 0..8 guardados para os junglers
JG_EVENTS_UNTIL = 600_000  # abates/mortes do jungler até 10:00

MONSTER_CODE = {"DRAGON": "D", "BARON_NASHOR": "B", "RIFTHERALD": "H", "HORDE": "G", "ATAKHAN": "A"}


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def _champ_names() -> dict:
    p = STATIC / "champions.json"
    return {int(c["id"]): c["name"] for c in load_json(p)} if p.exists() else {}


# ============================================================================ extração compacta
def compact(m: dict, t: dict, names: dict | None = None) -> dict | None:
    """match_v5 + timeline_v5 -> registro compacto (sem identificação de jogadores).
    Devolve None para remakes, mapas que não são o Summoner's Rift ou dados incompletos."""
    info = m["info"]
    if info.get("mapId") != 11 or (info.get("gameDuration") or 0) < 300:
        return None
    parts = {p["participantId"]: p for p in info["participants"]}
    if any(p.get("gameEndedInEarlySurrender") for p in parts.values()):
        return None
    win = next((tm["teamId"] for tm in info.get("teams", []) if tm.get("win")), None)
    if win not in (100, 200):
        return None
    names = names if names is not None else _champ_names()
    team = {pid: p["teamId"] for pid, p in parts.items()}

    def champ(p):
        cid = p.get("championId")
        return [cid, names.get(cid) or p.get("championName")]

    frames_raw = t["info"]["frames"]
    frames = []
    for f in frames_raw:
        row = [f["timestamp"]]
        for tm in (100, 200):
            g = xp = cs = lv = 0
            for k, v in f["participantFrames"].items():
                if team.get(int(k)) != tm:
                    continue
                g += v.get("totalGold", 0)
                xp += v.get("xp", 0)
                cs += v.get("minionsKilled", 0) + v.get("jungleMinionsKilled", 0)
                lv += v.get("level", 0)
            row += [g, xp, cs, lv]
        frames.append(row)

    events, kills = [], []
    for f in frames_raw:
        for e in f.get("events", []):
            ty, ts = e.get("type"), e.get("timestamp", 0)
            if ty == "CHAMPION_KILL":
                vt = team.get(e.get("victimId"))
                if vt:
                    events.append([ts, "K", 300 - vt])
                    kills.append(e)
            elif ty == "ELITE_MONSTER_KILL":
                kt = e.get("killerTeamId") or team.get(e.get("killerId"))
                code = MONSTER_CODE.get(e.get("monsterType"))
                if e.get("monsterSubType") == "ELDER_DRAGON":
                    code = "E"
                if kt in (100, 200) and code:
                    events.append([ts, code, kt])
            elif ty in ("BUILDING_KILL", "TURRET_PLATE_DESTROYED"):
                owner = e.get("teamId")
                if owner not in (100, 200):
                    continue
                code = "P" if ty == "TURRET_PLATE_DESTROYED" else (
                    "I" if e.get("buildingType") == "INHIBITOR_BUILDING" else "T")
                events.append([ts, code, 300 - owner])

    champs = {"100": {}, "200": {}}
    for p in parts.values():
        pos = p.get("teamPosition") or ""
        if pos:
            champs[str(p["teamId"])][pos] = champ(p)

    jungle = []
    for tm in (100, 200):
        cands = [p for p in parts.values() if p["teamId"] == tm and p.get("teamPosition") == "JUNGLE"]
        if len(cands) != 1:
            cands = [p for p in parts.values() if p["teamId"] == tm and 11 in (p.get("summoner1Id"), p.get("summoner2Id"))]
        if len(cands) != 1:
            continue
        p = cands[0]
        pid = p["participantId"]
        pos, jcs, lvl = [], [], []
        for f in frames_raw[:JG_FRAMES]:
            v = f["participantFrames"].get(str(pid)) or {}
            q = v.get("position") or {}
            pos.append([q.get("x"), q.get("y")])
            jcs.append(v.get("jungleMinionsKilled", 0))
            lvl.append(v.get("level", 0))
        td, deaths = [], []
        for e in kills:
            ts = e.get("timestamp", 0)
            if ts > JG_EVENTS_UNTIL:
                break
            q = e.get("position") or {}
            if e.get("killerId") == pid:
                td.append([ts, q.get("x"), q.get("y"), "k"])
            elif pid in (e.get("assistingParticipantIds") or []):
                td.append([ts, q.get("x"), q.get("y"), "a"])
            if e.get("victimId") == pid:
                deaths.append([ts, q.get("x"), q.get("y")])
        jungle.append({"team": tm, "champ": champ(p), "win": tm == win, "pos": pos, "jcs": jcs, "lvl": lvl,
                       "td": td[:4], "deaths": deaths[:3]})

    ver = (info.get("gameVersion") or "").split(".")
    return {
        "v": SAMPLE_VERSION, "gameId": info["gameId"], "queue": info.get("queueId"),
        "patch": ".".join(ver[:2]), "creation": info.get("gameCreation"), "duration": info.get("gameDuration"),
        "win": win, "champs": champs, "frames": frames, "events": events, "jungle": jungle,
    }


# ============================================================================ estado (features)
def _state(frames: list, events: list, ts: float) -> dict:
    """frames: [[ts, g100, xp100, cs100, lv100, g200, xp200, cs200, lv200], ...];
    events: [[ts, code, team], ...]. Devolve o estado no instante ts, do ponto de vista do time 100."""
    ts = max(0, ts)
    if ts >= frames[-1][0]:
        cur = frames[-1]
    else:
        i = 0
        while i + 1 < len(frames) and frames[i + 1][0] <= ts:
            i += 1
        a, b = frames[i], frames[min(i + 1, len(frames) - 1)]
        span = b[0] - a[0]
        w = 0.0 if span <= 0 else (ts - a[0]) / span
        cur = [a[k] + w * (b[k] - a[k]) for k in range(len(a))]
    c = {100: Counter(), 200: Counter()}
    last = {}
    for ets, code, tm in events:
        if ets > ts:
            continue
        c[tm][code] += 1
        if code in ("B", "E"):
            last[(code, tm)] = max(last.get((code, tm), -1e12), ets)
        if code == "I":
            if ets > ts - INHIB_RESPAWN_MS:
                c[tm]["Idown"] += 1
        if code == "D" and c[tm]["D"] == 4 and not c[300 - tm]["soul"]:
            c[tm]["soul"] = 1

    def diff(k):
        return c[100][k] - c[200][k]

    def active(code, dur):
        a = int(ts - last.get((code, 100), -1e12) <= dur)
        b = int(ts - last.get((code, 200), -1e12) <= dur)
        return a - b

    return {
        "perspective": 100, "ts": int(ts), "time": mmss(ts), "minute": round(ts / 60000, 3),
        "gold": round(cur[1] - cur[5]), "xp": round(cur[2] - cur[6]), "cs": round(cur[3] - cur[7]),
        "level": round((cur[4] - cur[8]) / 5, 3), "goldTotal": round(cur[1] + cur[5]),
        "kills": diff("K"), "towers": diff("T"), "inhibs": diff("I"), "inhibsDown": diff("Idown"),
        "plates": diff("P"), "dragons": diff("D"), "soul": diff("soul"), "elders": diff("E"),
        "elderActive": active("E", ELDER_BUFF_MS), "baronActive": active("B", BARON_BUFF_MS),
        "barons": diff("B"), "heralds": diff("H"), "grubs": diff("G"), "atakhan": diff("A"),
    }


def _m(s):
    return s["minute"] / 30.0


FEATURES = {
    "bias": lambda s: 1.0,
    "m": _m,
    "gold": lambda s: s["gold"] / 1000,
    "gold_m": lambda s: s["gold"] / 1000 * _m(s),
    "xp": lambda s: s["xp"] / 1000,
    "xp_m": lambda s: s["xp"] / 1000 * _m(s),
    "cs": lambda s: s["cs"] / 10,
    "kills": lambda s: s["kills"] / 5,
    "towers": lambda s: s["towers"],
    "towers_m": lambda s: s["towers"] * _m(s),
    "inhibs": lambda s: s["inhibs"],
    "inhibsDown": lambda s: s["inhibsDown"],
    "plates": lambda s: s["plates"],
    "dragons": lambda s: s["dragons"],
    "soul": lambda s: s["soul"],
    "elderActive": lambda s: s["elderActive"],
    "baronActive": lambda s: s["baronActive"],
    "barons": lambda s: s["barons"],
    "heralds": lambda s: s["heralds"],
    "grubs": lambda s: s["grubs"],
    "atakhan": lambda s: s["atakhan"],
}


def featurize(state: dict, names: list) -> list:
    return [FEATURES[n](state) for n in names]


def _sig(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


# ============================================================================ API importável
_MODEL = None


def load_model(path=None) -> dict:
    global _MODEL
    if _MODEL is None or path:
        p = path or MODEL
        if not p.exists():
            raise SystemExit("Modelo não encontrado. Rode: python tools/winprob.py train")
        _MODEL = load_json(p)
    return _MODEL


def win_prob(state: dict, team: int = 100) -> float:
    """Chance de vitória do time `team` (100/200) num estado de state_at (ponto de vista do azul)."""
    mdl = load_model()
    x = featurize(state, mdl["features"])
    p = _sig(sum(w * v for w, v in zip(mdl["weights"], x)))
    if state.get("perspective", 100) != 100:
        p = 1 - p
    return p if int(team) == 100 else 1 - p


def _summary_series(s: dict):
    frames = []
    for r in s["perMinute"]:
        t1, t2 = r["teams"].get("100", {}), r["teams"].get("200", {})
        lv = {100: 0, 200: 0}
        team_of = {str(p["pid"]): p["team"] for p in s["participants"]}
        for pid, v in r["players"].items():
            tm = team_of.get(str(pid))
            if tm:
                lv[tm] += v.get("level") or 0
        frames.append([r["ts"], t1.get("gold", 0), t1.get("xp", 0), t1.get("cs", 0), lv[100],
                       t2.get("gold", 0), t2.get("xp", 0), t2.get("cs", 0), lv[200]])
    events = []
    for k in s["events"]["kills"]:
        if k.get("victimTeam") in (100, 200):
            events.append([k["ts"], "K", 300 - k["victimTeam"]])
    for o in s["events"]["objectives"]:
        tm = o.get("killerTeam")
        if tm not in (100, 200):
            continue
        ty = o.get("type")
        if ty == "DRAGON":
            code = "E" if "ancião" in (o.get("name") or "") else "D"
        elif ty == "TOWER":
            code = "T"
        elif ty == "INHIBITOR":
            code = "I"
        elif ty == "TURRET_PLATE":
            code = "P"
        else:
            code = MONSTER_CODE.get(ty)
        if code:
            events.append([o["ts"], code, tm])
    events.sort(key=lambda e: e[0])
    return frames, events


def _to_ms(t) -> int:
    if isinstance(t, (int, float)) and t < 200:
        return int(t * 60000)   # minuto
    return parse_time(t)        # '15:31', '15' (minuto) ou ms


def state_at(s, minute_or_ts) -> dict:
    """Estado (ponto de vista do time 100) a partir de um resumo do projeto (dict ou gameId).
    minute_or_ts: '15:31', '15' / 15 (minuto; números < 200 são minutos) ou milissegundos."""
    if not isinstance(s, dict):
        from summarize import get_summary
        s = get_summary(int(s))
    frames, events = _summary_series(s)
    return _state(frames, events, _to_ms(minute_or_ts))


def win_prob_at(s, minute_or_ts, team: int = 100) -> float:
    return win_prob(state_at(s, minute_or_ts), team)


def prob_fn(s: dict, team: int = 100):
    """Função ts(ms) -> chance de vitória do `team` nesta partida, ou None se não houver modelo treinado.
    Para quem só quer enriquecer a saída (revisão de decisões, render): nunca levanta erro por falta de modelo."""
    try:
        if not MODEL.exists():
            return None
        load_model()
        frames, events = _summary_series(s)
    except (Exception, SystemExit):
        return None
    if not frames:
        return None
    return lambda ts: win_prob(_state(frames, events, max(0, int(ts))), team)


def enemy_jungle_pattern(champion: str) -> dict | None:
    """Padrão do campeão no elo (jungle_patterns.json), ou None se não houver o arquivo/campeão."""
    if not JUNGLE.exists():
        return None
    return load_json(JUNGLE).get("champions", {}).get(champion)


def enemy_jungle_compare(s: dict, team: int) -> dict | None:
    """Começo de jogo do jungler inimigo nesta partida × o padrão do campeão no elo.
    Linhas [métrica, nesta partida, no elo]; None se faltar jungler, padrão ou amostra (< 5 partidas)."""
    jg = next((p for p in s["participants"] if p["team"] != team and p.get("role") == "JUNGLE"), None)
    if not jg or s.get("mapId") != 11:
        return None
    pat = enemy_jungle_pattern(jg["champion"])
    if not pat or pat["games"] < 5:
        return None
    pid, jt = str(jg["pid"]), jg["team"]
    pos = [(r["players"][pid].get("x"), r["players"][pid].get("y")) for r in s["perMinute"][:6]]

    def top(d: dict, k: int = 3) -> str:
        return ", ".join(f"{a} {round(100 * v)}%" for a, v in list(d.items())[:k])

    rows = []
    if len(pos) > 1:
        rows.append(["campo no minuto 1", _camp(*pos[1], jt), top(pat["startCamp"])])
    for mi in (2, 3):
        if mi < len(pos) and str(mi) in pat["byMinute"]:
            rows.append([f"lado no minuto {mi}", _side(*pos[mi]), top(pat["byMinute"][str(mi)]["side"])])
    ks = sorted((k for k in s["events"]["kills"] if k["ts"] <= 600000 and jg["pid"] in {k["killer"], *k["assists"]}),
                key=lambda k: k["ts"])
    ft = pat["firstTakedown"]
    elo = (f'mediana {ft["medianTime"]}; antes de 4:00 em {round(100 * ft["before4min"])}% das partidas; '
           f'lado: {top(ft["side"])}') if ft.get("medianTime") else "sem abate até 10:00 na amostra"
    if ks:
        k = ks[0]
        this = f'{mmss(k["ts"])}, {_side(k["x"], k["y"])}' + (" (matou)" if k["killer"] == jg["pid"] else " (assistência)")
    else:
        this = "nenhuma até 10:00"
    rows.append(["1ª participação em abate", this, elo])
    obj = next((o for o in s["events"]["objectives"] if o["ts"] <= 600000 and o.get("killerTeam") == jt
                and o["type"] in ("DRAGON", "HORDE", "RIFTHERALD")), None)
    rows.append(["1º objetivo do time dele até 10:00", obj["name"] if obj else "nenhum", top(pat["firstTeamObjective"])])
    return {"champion": jg["champion"], "games": pat["games"], "winRate": pat["winRate"],
            "sample": load_json(JUNGLE).get("games"), "rows": rows}


def win_curve(s, team: int = 100, step_ms: int = 60000) -> list:
    """[(ts, p), ...] a cada step_ms até o fim (o último ponto é o fim da partida)."""
    if not isinstance(s, dict):
        from summarize import get_summary
        s = get_summary(int(s))
    frames, events = _summary_series(s)
    end = frames[-1][0]
    out, ts = [], 0
    while ts < end:
        out.append((ts, win_prob(_state(frames, events, ts), team)))
        ts += step_ms
    out.append((end, win_prob(_state(frames, events, end), team)))
    return out


# ============================================================================ crawl
def _read_seen() -> set:
    if not SEEN.exists():
        return set()
    return {int(x) for x in SEEN.read_text(encoding="utf-8").split() if x.strip().isdigit()}


def _append(path, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cmd_crawl(args) -> None:
    WP.mkdir(parents=True, exist_ok=True)
    queues = {int(q) for q in args.queue.split(",")}
    seen = _read_seen()
    names = _champ_names()
    added_cache = 0
    seeds = []
    # 1) partidas já no cache local
    for d in sorted(MATCHES.iterdir()) if MATCHES.exists() else []:
        mp, tp = d / "match_v5.json", d / "timeline_v5.json"
        if not (d.name.isdigit() and mp.exists()):
            continue
        m = load_json(mp)
        for p in m["info"]["participants"]:
            if p.get("puuid"):
                seeds.append(p["puuid"])
        gid = int(d.name)
        if gid in seen or not tp.exists() or m["info"].get("queueId") not in queues:
            continue
        rec = compact(m, load_json(tp), names)
        if rec:
            _append(SAMPLES, json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            added_cache += 1
        _append(SEEN, str(gid))
        seen.add(gid)
    log(f"[crawl] cache local: {added_cache} partidas novas adicionadas")
    if args.max <= 0:
        return
    try:
        api = RiotAPI()
    except RiotAPIError as e:
        raise SystemExit(f"[crawl] {e}")
    # semente: jogadores das partidas do cache (só em memória; nunca gravados)
    seeds = list(dict.fromkeys(seeds))
    random.Random(7).shuffle(seeds)
    frontier = deque((p, 0) for p in seeds)
    queued = set(seeds)
    new = fails = 0
    t0 = time.monotonic()
    while frontier and new < args.max:
        puuid, depth = frontier.popleft()
        try:
            ids = api.get(api.routing, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                          {"type": "ranked", "start": 0, "count": args.per_player})
        except RiotAPIError as e:
            if any(c in str(e) for c in ("HTTP 401", "HTTP 403")):
                raise SystemExit(f"[crawl] {e}; parando (retome depois com o mesmo comando)")
            fails += 1
            continue
        for mid in ids or []:
            if new >= args.max:
                break
            if not mid.startswith("BR1_"):
                continue
            gid = int(mid.split("_", 1)[1])
            if gid in seen:
                continue
            try:
                m = api.match(mid)
                if m["info"].get("queueId") not in queues:
                    _append(SEEN, str(gid))
                    seen.add(gid)
                    continue
                tl = api.timeline(mid)
            except RiotAPIError as e:
                if any(c in str(e) for c in ("HTTP 401", "HTTP 403")):
                    raise SystemExit(f"[crawl] {e}; parando (retome depois com o mesmo comando)")
                fails += 1
                log(f"[crawl] {gid}: {e}")
                continue
            if depth < args.depth:
                for p in m["info"]["participants"]:
                    pu = p.get("puuid")
                    if pu and pu not in queued:
                        queued.add(pu)
                        frontier.append((pu, depth + 1))
            rec = compact(m, tl, names)
            if rec:
                _append(SAMPLES, json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
                new += 1
            _append(SEEN, str(gid))
            seen.add(gid)
            if new and new % 10 == 0 and rec:
                el = time.monotonic() - t0
                log(f"[crawl] {new}/{args.max} novas ({el / 60:.1f} min; ~{el / new * (args.max - new) / 60:.0f} min restantes;"
                    f" fila de jogadores {len(frontier)}; falhas {fails})")
    log(f"[crawl] fim: {new} partidas novas da API, {added_cache} do cache; falhas {fails}; total em seen.txt {len(seen)}")


# ============================================================================ treino
def load_samples(queues=None) -> list:
    if not SAMPLES.exists():
        raise SystemExit("Sem amostras. Rode: python tools/winprob.py crawl --max 1000")
    out, ids = [], set()
    with open(SAMPLES, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["gameId"] in ids or (queues and r.get("queue") not in queues):
                continue
            ids.add(r["gameId"])
            out.append(r)
    return out


def is_test(gid: int, holdout: float) -> bool:
    return (zlib.crc32(str(gid).encode()) % 10000) < holdout * 10000


def game_rows(r: dict, names: list):
    """Linhas de treino: um frame por minuto, sem o minuto 0 e sem o frame final (fim de jogo)."""
    fr, ev = r["frames"], r["events"]
    y = 1 if r["win"] == 100 else 0
    for row in fr[1:-1]:
        st = _state(fr, ev, row[0])
        yield featurize(st, names), y, st["minute"]


def _solve(A, b):
    n = len(b)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                Mr, Mc = M[r], M[c]
                for k in range(c, n + 1):
                    Mr[k] -= f * Mc[k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_logistic(X, y, l2: float = 1.0, iters: int = 30, verbose: bool = True):
    """Regressão logística por Newton-Raphson (IRLS) com penalidade L2 (o intercepto não é penalizado).
    Converge em poucas iterações; equivale a descida de gradiente com passo pela curvatura."""
    d = len(X[0])
    w = [0.0] * d
    nz = [[j for j, v in enumerate(x) if v] for x in X]
    for it in range(iters):
        g = [l2 * w[j] if j else 0.0 for j in range(d)]
        H = [[0.0] * d for _ in range(d)]
        for j in range(1, d):
            H[j][j] = l2
        ll = 0.0
        for x, yi, idx in zip(X, y, nz):
            z = 0.0
            for j in idx:
                z += w[j] * x[j]
            p = _sig(z)
            ll -= math.log(max(1e-12, p if yi else 1 - p))
            r, s = p - yi, p * (1 - p)
            for a in idx:
                xa = x[a]
                g[a] += r * xa
                sxa = s * xa
                Ha = H[a]
                for b in idx:
                    Ha[b] += sxa * x[b]
        step = _solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        mx = max(abs(si) for si in step)
        if verbose:
            log(f"[train] iteração {it + 1}: log loss {ll / len(X):.4f}  passo máx {mx:.2e}")
        if mx < 1e-6:
            break
    return w


def evaluate(w, rows) -> dict:
    n = len(rows)
    ll = acc = brier = 0.0
    bins = defaultdict(lambda: [0, 0.0, 0])
    per_min = defaultdict(lambda: [0, 0, 0.0])
    for x, yi, minute in rows:
        p = _sig(sum(a * b for a, b in zip(w, x)))
        ll -= math.log(max(1e-12, p if yi else 1 - p))
        acc += (p >= 0.5) == bool(yi)
        brier += (p - yi) ** 2
        b = min(9, int(p * 10))
        bins[b][0] += 1
        bins[b][1] += p
        bins[b][2] += yi
        mb = "01-10" if minute <= 10.5 else "11-15" if minute <= 15.5 else "16-20" if minute <= 20.5 else \
             "21-25" if minute <= 25.5 else "26-30" if minute <= 30.5 else "31+"
        per_min[mb][0] += 1
        per_min[mb][1] += (p >= 0.5) == bool(yi)
        per_min[mb][2] -= math.log(max(1e-12, p if yi else 1 - p))
    base = sum(r[1] for r in rows) / n
    base_ll = -(base * math.log(base) + (1 - base) * math.log(1 - base))
    return {
        "rows": n, "logLoss": round(ll / n, 4), "baselineLogLoss": round(base_ll, 4),
        "accuracy": round(acc / n, 4), "brier": round(brier / n, 4),
        "calibration": [{"bin": f"{b * 10}-{b * 10 + 10}%", "n": c[0], "predMean": round(c[1] / c[0], 3),
                         "observed": round(c[2] / c[0], 3)} for b, c in sorted(bins.items())],
        "byMinute": [{"minutes": k, "n": v[0], "accuracy": round(v[1] / v[0], 3), "logLoss": round(v[2] / v[0], 4)}
                     for k, v in sorted(per_min.items())],
    }


def cmd_train(args) -> None:
    queues = {int(q) for q in args.queue.split(",")} if args.queue else None
    games = load_samples(queues)
    names = list(FEATURES)
    tr, te = [], []
    n_tr = n_te = 0
    for r in games:
        rows = list(game_rows(r, names))
        if is_test(r["gameId"], args.holdout):
            te += rows
            n_te += 1
        else:
            tr += rows
            n_tr += 1
    log(f"[train] {len(games)} partidas ({n_tr} treino / {n_te} teste), {len(tr)} + {len(te)} linhas (minutos)")
    w = fit_logistic([r[0] for r in tr], [r[1] for r in tr], l2=args.l2)
    m_test = evaluate(w, te) if te else None
    m_train = evaluate(w, tr)
    # modelo final: mesmo procedimento com todas as partidas
    allrows = tr + te
    w_all = fit_logistic([r[0] for r in allrows], [r[1] for r in allrows], l2=args.l2, verbose=False) if te else w
    model = {
        "version": 1, "createdAt": dt.datetime.now().isoformat(timespec="seconds"),
        "features": names, "weights": [round(v, 6) for v in w_all],
        "weightsTrainSplit": [round(v, 6) for v in w],
        "l2": args.l2, "holdout": args.holdout, "queues": sorted(queues) if queues else "todas",
        "games": len(games), "gamesTrain": n_tr, "gamesTest": n_te,
        "patches": dict(Counter(r.get("patch") for r in games).most_common()),
        "blueWinRate": round(sum(r["win"] == 100 for r in games) / len(games), 4),
        "metricsTest": m_test, "metricsTrain": {k: m_train[k] for k in ("rows", "logLoss", "accuracy", "brier")},
        "notes": "Perspectiva do time 100 (azul); label = azul venceu. Linhas = frames de minuto (sem o 0 e sem o final). "
                 "Split treino/teste por partida (crc32 do gameId). 'weights' = refeito com todas as partidas.",
    }
    save_json(MODEL, model, indent=1)
    print("feature\tpeso")
    for n_, v in zip(names, w_all):
        print(f"{n_}\t{v:+.4f}")
    if m_test:
        print()
        print("métrica (teste)\tvalor")
        for k in ("rows", "logLoss", "baselineLogLoss", "accuracy", "brier"):
            print(f"{k}\t{m_test[k]}")
        print()
        print("faixa\tn\tprevisto\tobservado")
        for c in m_test["calibration"]:
            print(f'{c["bin"]}\t{c["n"]}\t{c["predMean"]}\t{c["observed"]}')
        print()
        print("minutos\tn\tacurácia\tlogLoss")
        for c in m_test["byMinute"]:
            print(f'{c["minutes"]}\t{c["n"]}\t{c["accuracy"]}\t{c["logLoss"]}')
    log(f"[train] modelo gravado em {MODEL.relative_to(DATA.parent)}")


# ============================================================================ curva / estado
def _team_arg(s: dict, team: str) -> int:
    if team in ("100", "200"):
        return int(team)
    from common import resolve_player
    p = resolve_player(s["participants"], team)
    if p is None:
        log(f"[aviso] jogador '{team}' não está na partida; usando o time 100")
        return 100
    return p["team"]


def cmd_curve(args) -> None:
    from summarize import get_summary
    s = get_summary(int(args.game))
    team = _team_arg(s, args.team)
    frames, events = _summary_series(s)
    print("minuto\ttempo\tpTime\tdelta\touroTime\tabatesTime\ttorresTime\tdragõesTime")
    prev = None
    end = frames[-1][0]
    ts_list = list(range(0, int(end), args.step * 1000)) + [end]
    sign = 1 if team == 100 else -1
    for ts in ts_list:
        st = _state(frames, events, ts)
        p = win_prob(st, team)
        d = "" if prev is None else f"{(p - prev) * 100:+.1f}"
        print(f'{st["minute"]:.1f}\t{st["time"]}\t{p * 100:.1f}\t{d}\t{sign * st["gold"]:+d}\t'
              f'{sign * st["kills"]:+d}\t{sign * st["towers"]:+d}\t{sign * st["dragons"]:+d}')
        prev = p
    log(f"[curve] chance de vitória do time {team} (resultado real: venceu {s.get('winnerTeam')})")


def cmd_state(args) -> None:
    from summarize import get_summary
    s = get_summary(int(args.game))
    team = _team_arg(s, args.team)
    st = state_at(s, args.t)
    print("campo\tvalor")
    for k, v in st.items():
        print(f"{k}\t{v}")
    print(f"pTime{team}\t{win_prob(st, team) * 100:.1f}")


# ============================================================================ junglers
def _side(x, y) -> str:
    if x is None or y is None:
        return "?"
    z = zone_abs(x, y)
    if z.startswith("base"):
        return "base"
    return "top" if y - x > 1200 else "bot" if y - x < -1200 else "centro"


def _rel_zone(x, y, team) -> str:
    return zone_rel(x, y, team) if x is not None and y is not None else "?"


def _camp(x, y, team) -> str:
    from landmarks import nearest
    if x is None:
        return "?"
    c = nearest(x, y, 1, kind="campo")[0]
    base = c["name"].rsplit(" ", 1)[0]
    return f'{base} {"própria" if c["team"] == team else "inimiga"}'


def _pct(c: Counter, n: int) -> dict:
    return {k: round(v / n, 3) for k, v in c.most_common()}


def build_jungle_patterns(games: list) -> dict:
    by = defaultdict(list)
    for r in games:
        for j in r.get("jungle", []):
            by[j["champ"][1]].append((r, j))
    out = {}
    for champ, lst in by.items():
        n = len(lst)
        mins = {}
        for mi in range(1, 6):
            side, zone = Counter(), Counter()
            jcs = []
            for r, j in lst:
                if mi < len(j["pos"]):
                    x, y = j["pos"][mi]
                    side[_side(x, y)] += 1
                    zone[_rel_zone(x, y, j["team"])] += 1
                    jcs.append(j["jcs"][mi])
            k = sum(side.values()) or 1
            mins[str(mi)] = {"side": _pct(side, k), "zone": _pct(zone, k),
                             "jungleCsAvg": round(sum(jcs) / len(jcs), 1) if jcs else None}
        start = Counter(_camp(*(j["pos"][1] if len(j["pos"]) > 1 else (None, None)), j["team"]) for r, j in lst)
        ftd = [j["td"][0] for r, j in lst if j["td"]]
        ftd_zone = Counter(_rel_zone(t[1], t[2], j["team"]) for (r, j) in lst for t in j["td"][:1])
        ftd_side = Counter(_side(t[1], t[2]) for t in ftd)
        times = sorted(t[0] for t in ftd)
        fdeath = [j["deaths"][0][0] for r, j in lst if j["deaths"]]
        # primeiro objetivo épico do time do jungler até 10:00
        first_obj = Counter()
        for r, j in lst:
            ob = next((e for e in r["events"] if e[1] in ("D", "G", "H") and e[2] == j["team"] and e[0] <= 600000), None)
            first_obj[{"D": "dragão", "G": "vastilarvas", "H": "arauto"}[ob[1]] if ob else "nenhum até 10:00"] += 1
        out[champ] = {
            "games": n, "winRate": round(sum(j["win"] for r, j in lst) / n, 3),
            "side": {"blue": sum(j["team"] == 100 for r, j in lst), "red": sum(j["team"] == 200 for r, j in lst)},
            "startCamp": _pct(start, n),
            "byMinute": mins,
            "firstTakedown": {
                "gamesWithTakedownBy10": len(ftd),
                "before4min": round(sum(t < 240000 for t in times) / n, 3),
                "before6min": round(sum(t < 360000 for t in times) / n, 3),
                "medianTime": mmss(times[len(times) // 2]) if times else None,
                "zone": _pct(ftd_zone, len(ftd) or 1), "side": _pct(ftd_side, len(ftd) or 1),
                "asKiller": round(sum(t[3] == "k" for t in ftd) / (len(ftd) or 1), 3),
            },
            "firstDeathBefore10": round(len(fdeath) / n, 3),
            "firstTeamObjective": _pct(first_obj, n),
        }
    return {"generatedAt": dt.datetime.now().isoformat(timespec="seconds"), "games": len(games),
            "notes": "Posições só a cada ~60 s (frame do minuto). side: top = metade do barão/vastilarvas "
                     "(y - x > 1200), bot = metade do dragão (y - x < -1200). Zonas relativas ao time do jungler. "
                     "Primeira participação = primeiro abate ou assistência do jungler até 10:00.",
            "champions": dict(sorted(out.items(), key=lambda kv: -kv[1]["games"]))}


def _fmt_pct(d: dict, top: int = 4) -> str:
    return ", ".join(f"{k} {v * 100:.0f}%" for k, v in list(d.items())[:top])


def cmd_junglers(args) -> None:
    if args.rebuild or not JUNGLE.exists() or (SAMPLES.exists() and SAMPLES.stat().st_mtime > JUNGLE.stat().st_mtime):
        pats = build_jungle_patterns(load_samples())
        save_json(JUNGLE, pats, indent=1)
        log(f"[junglers] {len(pats['champions'])} campeões, {pats['games']} partidas -> {JUNGLE.relative_to(DATA.parent)}")
    else:
        pats = load_json(JUNGLE)
    champs = pats["champions"]
    if not args.champ:
        print("campeão\tpartidas\twinrate\tinício\tlado_min3\t1ª_participação_mediana\tantes_4min\tzona_1ª_participação")
        for c, v in champs.items():
            if v["games"] < args.min_games:
                continue
            ft = v["firstTakedown"]
            print(f'{c}\t{v["games"]}\t{v["winRate"] * 100:.0f}%\t{_fmt_pct(v["startCamp"], 2)}\t'
                  f'{_fmt_pct(v["byMinute"]["3"]["side"], 3)}\t{ft["medianTime"] or "-"}\t{ft["before4min"] * 100:.0f}%\t'
                  f'{_fmt_pct(ft["zone"], 3)}')
        return
    from common import _norm
    key = next((c for c in champs if _norm(c) == _norm(args.champ)), None)
    if key is None:
        raise SystemExit(f"{args.champ}: nenhum jungler com esse campeão nas amostras")
    v = champs[key]
    if args.json:
        print(json.dumps({key: v}, ensure_ascii=False))
        return
    ft = v["firstTakedown"]
    print("item\tvalor")
    print(f'campeão\t{key}')
    print(f'partidas\t{v["games"]} (azul {v["side"]["blue"]}, vermelho {v["side"]["red"]}); winrate {v["winRate"] * 100:.0f}%')
    print(f'campo no minuto 1 (início)\t{_fmt_pct(v["startCamp"], 4)}')
    for mi in ("2", "3", "4"):
        b = v["byMinute"][mi]
        print(f'minuto {mi}: lado\t{_fmt_pct(b["side"])}')
        print(f'minuto {mi}: zona\t{_fmt_pct(b["zone"])}; CS de jungle médio {b["jungleCsAvg"]}')
    print(f'1ª participação em abate (até 10:00)\t{ft["gamesWithTakedownBy10"]}/{v["games"]} partidas; mediana {ft["medianTime"]};'
          f' antes de 4:00 {ft["before4min"] * 100:.0f}%; antes de 6:00 {ft["before6min"] * 100:.0f}%; como matador {ft["asKiller"] * 100:.0f}%')
    print(f'1ª participação: zona\t{_fmt_pct(ft["zone"])}')
    print(f'1ª participação: lado\t{_fmt_pct(ft["side"])}')
    print(f'morreu antes de 10:00\t{v["firstDeathBefore10"] * 100:.0f}%')
    print(f'1º objetivo do time até 10:00\t{_fmt_pct(v["firstTeamObjective"])}')
    if v["games"] < 10:
        log(f"[aviso] amostra pequena ({v['games']} partidas)")


# ============================================================================ main
def main() -> None:
    ap = argparse.ArgumentParser(description="Chance de vitória por minuto e padrões de jungle.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl", help="baixa partidas ranqueadas de jogadores do mesmo elo e extrai features")
    c.add_argument("--max", type=int, default=1000, help="partidas novas da API (0 = só o cache local)")
    c.add_argument("--queue", default="420,440")
    c.add_argument("--per-player", type=int, default=10, help="ids recentes por jogador")
    c.add_argument("--depth", type=int, default=1, help="0 = só jogadores das suas partidas; 1 = também os das novas")
    c = sub.add_parser("train", help="treina a regressão logística")
    c.add_argument("--holdout", type=float, default=0.2)
    c.add_argument("--l2", type=float, default=1.0)
    c.add_argument("--queue", default="", help="filtra filas (ex.: 420,440)")
    c = sub.add_parser("curve", help="chance de vitória por minuto numa partida")
    c.add_argument("game")
    c.add_argument("--team", default="me", help="me | 100 | 200 | jogador")
    c.add_argument("--step", type=int, default=60, help="segundos entre pontos")
    c = sub.add_parser("state", help="estado + chance num instante")
    c.add_argument("game")
    c.add_argument("t")
    c.add_argument("--team", default="me")
    c = sub.add_parser("junglers", help="padrões de jungle por campeão (minutos 1-5)")
    c.add_argument("--champ")
    c.add_argument("--min-games", type=int, default=5)
    c.add_argument("--rebuild", action="store_true")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args()
    {"crawl": cmd_crawl, "train": cmd_train, "curve": cmd_curve, "state": cmd_state,
     "junglers": cmd_junglers}[a.cmd](a)


if __name__ == "__main__":
    main()
