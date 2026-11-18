"""Scout dos jogadores da partida atual, para o pré-jogo (`lol pregame`).

Tudo pela API local do cliente (LCU, só GET), que responde também sobre os outros jogadores da partida e não gasta
o limite da chave da Riot:
  - /lol-match-history/v1/products/lol/{puuid}/matches   últimas ~20 partidas do jogador (todas as filas);
  - /lol-match-history/v1/games/{gameId} e /game-timelines/{gameId}   partida e timeline (para o pathing do jungler);
  - /lol-ranked/v1/ranked-stats/{puuid}                   elo, PdL, vitórias e derrotas da temporada;
  - /lol-champion-mastery/v1/{puuid}/champion-mastery     maestria por campeão.
Os puuids (do cliente) vêm de /lol-gameflow/v1/session, que existe no carregamento e com o jogo aberto.

Ordem (o mais útil primeiro, com limite de tempo): 1) o jungler inimigo, com o começo de jogo das últimas partidas
dele de jungle (mesma extração do `winprob crawl`); 2) elo, maestria e partidas recentes dos inimigos; 3) dos aliados.

Histórico profundo do jungler (com chave da Riot e sem o crawl rodando): pela Match-V5, as últimas ~20 ranqueadas
de jungle dele (`type=ranked`, teamPosition JUNGLE), em vez das ~8 que cabem nas últimas 20 partidas do cliente.
São ~40-60 chamadas, que saem em segundos com o limitador da RiotAPI. Com o crawl rodando (data/winprob/nightly.lock),
os dois disputariam o mesmo limite: fica o caminho pelo cliente.

Nada de nome, Riot ID ou PUUID é gravado: o resultado fica em memória e vai para a página/terminal pelo campeão.
Tudo é amostra pequena de uma pessoa (≤ 20 partidas): mostre sempre o n.
"""
from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from common import LCU, LCUError, mmss

SR_QUEUES = {400, 420, 430, 440, 490}   # Normal, Solo/Duo, Às cegas, Flex, Quickplay (Summoner's Rift)
SMITE = 11
JG_GAMES = 8                              # partidas de jungle do jungler inimigo analisadas pelo cliente
JG_API_GAMES = 20                         # ... e pela Riot API (histórico profundo)
JG_API_IDS = 60                           # ranqueadas olhadas, no máximo, para achar essas partidas de jungle
QUEUE_TYPE = {420: "RANKED_SOLO_5x5", 440: "RANKED_FLEX_SR"}
TIER_PT = {"IRON": "Ferro", "BRONZE": "Bronze", "SILVER": "Prata", "GOLD": "Ouro", "PLATINUM": "Platina",
           "EMERALD": "Esmeralda", "DIAMOND": "Diamante", "MASTER": "Mestre", "GRANDMASTER": "Grão-Mestre",
           "CHALLENGER": "Desafiante"}


# ----------------------------------------------------------------------------- leitura
def identities(lcu: LCU) -> dict[int, dict]:
    """championId -> {"puuid", "team"} pela sessão do gameflow (carregamento ou jogo aberto). {} se não houver."""
    try:
        ses = lcu.get("/lol-gameflow/v1/session") or {}
    except LCUError:
        return {}
    gd = ses.get("gameData") or {}
    out = {}
    for team, rows in ((100, gd.get("teamOne") or []), (200, gd.get("teamTwo") or [])):
        for m in rows:
            cid, pu = int(m.get("championId") or 0), m.get("puuid")
            if cid and pu:
                out[cid] = {"puuid": pu, "team": team}
    return out


def history(lcu: LCU, puuid: str) -> list[dict]:
    """Últimas partidas do jogador (o cliente devolve ~20), da mais recente para a mais antiga, só com ele."""
    h = lcu.get(f"/lol-match-history/v1/products/lol/{puuid}/matches", {"begIndex": 0, "endIndex": 20}) or {}
    out = []
    for g in (h.get("games") or {}).get("games") or []:
        p = (g.get("participants") or [{}])[0]
        s = p.get("stats") or {}
        out.append({"gameId": g.get("gameId"), "queue": g.get("queueId"), "map": g.get("mapId"),
                    "creation": g.get("gameCreation"), "duration": g.get("gameDuration") or 0,
                    "champ": p.get("championId"), "pid": p.get("participantId"), "team": p.get("teamId"),
                    "smite": SMITE in (p.get("spell1Id"), p.get("spell2Id")), "win": bool(s.get("win")),
                    "k": s.get("kills", 0), "d": s.get("deaths", 0), "a": s.get("assists", 0)})
    out.sort(key=lambda r: r["creation"] or 0, reverse=True)
    return [r for r in out if (r["duration"] or 0) >= 300]   # sem remakes


def _as_v5(g: dict, tl: dict) -> tuple[dict, dict]:
    """Partida + timeline do LCU (formato v4) -> o formato Match-V5 que winprob.compact() entende."""
    teams = [{"teamId": t.get("teamId"), "win": t.get("win") == "Win"} for t in g.get("teams") or []]
    parts = [{"participantId": p.get("participantId"), "teamId": p.get("teamId"), "championId": p.get("championId"),
              "summoner1Id": p.get("spell1Id"), "summoner2Id": p.get("spell2Id"), "teamPosition": ""}
             for p in g.get("participants") or []]
    info = {"gameId": g.get("gameId"), "mapId": g.get("mapId"), "queueId": g.get("queueId"),
            "gameDuration": g.get("gameDuration"), "gameCreation": g.get("gameCreation"),
            "gameVersion": g.get("gameVersion") or "", "participants": parts, "teams": teams}
    return {"info": info}, {"info": {"frames": tl.get("frames") or []}}


def jungle_record(lcu: LCU, row: dict, names: dict) -> dict | None:
    """Uma partida de jungle do jogador -> registro no formato das amostras do crawl, só com o jungle dele."""
    from winprob import compact
    g = lcu.get(f"/lol-match-history/v1/games/{row['gameId']}")
    tl = lcu.get(f"/lol-match-history/v1/game-timelines/{row['gameId']}")
    m, t = _as_v5(g or {}, tl or {})
    rec = compact(m, t, names)
    if not rec:
        return None
    rec["jungle"] = [j for j in rec["jungle"] if j["team"] == row["team"]]
    return rec if rec["jungle"] else None


def crawl_running() -> bool:
    """O `winprob nightly` está rodando (lock com menos de 6 h)?"""
    try:
        from winprob import WP
    except ImportError:
        return False
    lock = WP / "nightly.lock"
    return lock.exists() and time.time() - lock.stat().st_mtime < 6 * 3600


def api_jungle_profile(lcu: LCU, puuid: str, cid: int, names: dict, deadline: float | None = None) -> dict | None:
    """Últimas ~20 ranqueadas de jungle do jogador pela Riot API. None se não houver chave, se o crawl estiver
    rodando ou se a API falhar (aí vale o caminho pelo cliente). O Riot ID só é usado em memória."""
    from common import RiotAPI, RiotAPIError
    from winprob import build_jungle_patterns, compact
    if crawl_running():
        return None
    try:
        api = RiotAPI(patient=False)
    except RiotAPIError:
        return None
    try:
        sm = lcu.get(f"/lol-summoner/v2/summoners/puuid/{puuid}") or {}
        if not (sm.get("gameName") and sm.get("tagLine")):
            return None
        pu = api.account_by_riot_id(sm["gameName"], sm["tagLine"])["puuid"]
        ids = api.get(api.routing, f"/lol/match/v5/matches/by-puuid/{pu}/ids",
                      {"type": "ranked", "start": 0, "count": JG_API_IDS}) or []
    except (LCUError, RiotAPIError):
        return None

    def get_match(mid):
        try:
            return api.match(mid)
        except RiotAPIError:
            return None

    jg, seen_sr, with_champ = [], 0, 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i in range(0, len(ids), 10):                    # em lotes, até achar JG_API_GAMES de jungle
            if len(jg) >= JG_API_GAMES or (deadline and time.monotonic() > deadline):
                break
            for mid, m in zip(ids[i:i + 10], ex.map(get_match, ids[i:i + 10])):
                if len(jg) >= JG_API_GAMES:
                    break
                info = (m or {}).get("info") or {}
                me = next((x for x in info.get("participants") or [] if x.get("puuid") == pu), None)
                if not me or info.get("mapId") != 11 or (info.get("gameDuration") or 0) < 300:
                    continue
                seen_sr += 1
                if me.get("teamPosition") == "JUNGLE":
                    jg.append((mid, m, me["teamId"]))
                    with_champ += me.get("championId") == cid

        def get_tl(row):
            try:
                return api.timeline(row[0])
            except RiotAPIError:
                return None
        tls = list(ex.map(get_tl, jg))
    recs = []
    for (mid, m, team), tl in zip(jg, tls):
        if not tl:
            continue
        rec = compact(m, tl, names)
        if rec:
            rec["jungle"] = [j for j in rec["jungle"] if j["team"] == team]
            if rec["jungle"]:
                recs.append(rec)
    out = {"source": "riot", "recentSR": seen_sr, "jungleGames": len(jg), "analyzed": len(recs),
           "withChamp": with_champ}
    if recs:
        allc = [{**r, "jungle": [{**r["jungle"][0], "champ": [0, "*"]}]} for r in recs]
        out["pattern"] = build_jungle_patterns(allc, recent=False)["champions"]["*"]
    return out


def ranked(lcu: LCU, puuid: str, queue: int | None) -> dict | None:
    r = lcu.get(f"/lol-ranked/v1/ranked-stats/{puuid}") or {}
    qm = r.get("queueMap") or {}
    q = qm.get(QUEUE_TYPE.get(queue or 420, "RANKED_SOLO_5x5")) or {}
    tier = q.get("tier")
    if not tier or tier in ("NONE", ""):
        return None
    div = q.get("division") if q.get("division") not in (None, "NA") else ""
    w, l = q.get("wins") or 0, q.get("losses") or 0
    return {"tier": tier, "label": f'{TIER_PT.get(tier, tier.title())} {div}'.strip(), "lp": q.get("leaguePoints"),
            "wins": w, "losses": l, "provisional": bool(q.get("isProvisional"))}


def mastery(lcu: LCU, puuid: str, cid: int) -> dict:
    lst = lcu.get(f"/lol-champion-mastery/v1/{puuid}/champion-mastery") or []
    m = next((x for x in lst if x.get("championId") == cid), None)
    if not m:
        return {"points": 0, "level": 0, "lastPlay": None}
    return {"points": m.get("championPoints") or 0, "level": m.get("championLevel") or 0,
            "lastPlay": m.get("lastPlayTime")}


# ----------------------------------------------------------------------------- análise
def jungler_profile(lcu: LCU, puuid: str, cid: int, names: dict, hist: list[dict] | None = None,
                    deadline: float | None = None) -> dict:
    """Começo de jogo do jungler nas últimas partidas de jungle dele (Smite, Summoner's Rift)."""
    from winprob import build_jungle_patterns
    hist = hist if hist is not None else history(lcu, puuid)
    sr = [r for r in hist if r["map"] == 11 and r["queue"] in SR_QUEUES]
    jg = [r for r in sr if r["smite"]]
    recs = []
    for row in jg[:JG_GAMES]:
        if deadline and time.monotonic() > deadline:
            break
        try:
            rec = jungle_record(lcu, row, names)
        except LCUError:
            continue
        if rec:
            recs.append(rec)
    out = {"source": "cliente", "recentSR": len(sr), "jungleGames": len(jg), "analyzed": len(recs),
           "withChamp": sum(r["champ"] == cid for r in jg)}
    if recs:
        allc = [{**r, "jungle": [{**r["jungle"][0], "champ": [0, "*"]}]} for r in recs]
        out["pattern"] = build_jungle_patterns(allc, recent=False)["champions"]["*"]
    return out


def quick_profile(lcu: LCU, puuid: str, cid: int, queue: int | None) -> dict:
    """Parte rápida (~0,3 s): elo da fila e maestria do campeão. O cliente não mostra as derrotas dos outros
    jogadores (vem 0), então só as vitórias da temporada valem."""
    try:
        rk = ranked(lcu, puuid, queue)
    except LCUError:
        rk = None
    try:
        ms = mastery(lcu, puuid, cid)
    except LCUError:
        ms = None
    return {"rank": rk, "mastery": ms}


def history_profile(hist: list[dict], cid: int) -> dict:
    """Parte lenta (o histórico leva ~7 s por jogador no cliente): campeão, role e sequência recentes."""
    sr = [r for r in hist if r["map"] == 11 and r["queue"] in SR_QUEUES]
    on = [r for r in sr if r["champ"] == cid]
    streak, last = 0, None
    for r in sr:
        if last is None or r["win"] == last:
            streak, last = streak + 1, r["win"]
        else:
            break
    return {"recentSR": len(sr), "smiteGames": sum(r["smite"] for r in sr),
            "champGames": len(on), "champWins": sum(r["win"] for r in on),
            "streak": {"n": streak, "win": last} if streak else None}


def alerts(pf: dict, role: str | None) -> list[str]:
    """Avisos curtos e verificáveis (cada um com o n de onde saiu). Sem histórico, só a maestria."""
    out = []
    ms = pf.get("mastery") or {}
    n, on = pf.get("recentSR"), pf.get("champGames")
    if pf.get("mastery") is not None and ms.get("points", 0) < 2000 and not on:
        out.append("primeira vez (ou quase) com o campeão")
    elif n and on >= max(8, n // 2):
        out.append(f"especialista: {on} das últimas {n} com o campeão")
    if n is not None and n >= 5:
        if role == "JUNGLE" and pf["smiteGames"] <= 1:
            out.append(f"fora da role: jungle em {pf['smiteGames']} das últimas {n}")
        elif role and role != "JUNGLE" and pf["smiteGames"] >= n * 0.7:
            out.append(f"costuma ser jungler ({pf['smiteGames']} das últimas {n})")
    st = pf.get("streak")
    if st and st["n"] >= 4:
        out.append(f'{st["n"]} {"vitórias" if st["win"] else "derrotas"} seguidas')
    return out


def scout(players: list[dict], queue: int | None, names: dict, budget: float = 45.0, log=None) -> dict:
    """players: [{"cid", "side": "enemy"|"ally", "role", "puuid", "me": bool}]. Em paralelo:
    (a) o jungler inimigo: histórico + as últimas partidas de jungle dele (o mais útil, ~15 s);
    (b) elo e maestria de todos (rápido);
    depois (c) o histórico dos outros (inimigos primeiro, ~7 s cada) até o limite de tempo. Devolve por campeão."""
    t0 = time.monotonic()
    deadline = t0 + budget
    lcu = LCU()
    out = {"players": {}, "jungler": None, "elapsed": None, "partial": False}
    rest = [p for p in players if p.get("puuid") and not p.get("me")]
    rest.sort(key=lambda p: (p["side"] != "enemy", p.get("role") != "JUNGLE"))   # jungler inimigo, inimigos, aliados
    ej = next((p for p in rest if p["side"] == "enemy" and p.get("role") == "JUNGLE"), None)
    hists = {}

    def jungler():
        deep = api_jungle_profile(lcu, ej["puuid"], ej["cid"], names, deadline)
        if deep and deep.get("analyzed", 0) >= 3:
            out["jungler"] = {"cid": ej["cid"], **deep}
        else:
            try:
                hists[ej["cid"]] = history(lcu, ej["puuid"])
                out["jungler"] = {"cid": ej["cid"], **jungler_profile(lcu, ej["puuid"], ej["cid"], names,
                                                                       hists[ej["cid"]], deadline)}
            except LCUError as e:
                out["jungler"] = {"cid": ej["cid"], "error": str(e)}
        if log:
            log(f"[scout] jungler inimigo em {time.monotonic() - t0:.1f} s "
                f"({out['jungler'].get('analyzed', 0)} partidas de jungle, fonte: {out['jungler'].get('source', '?')})")

    def quick():
        for p in rest:
            out["players"][p["cid"]] = {"side": p["side"], "role": p.get("role"),
                                        **quick_profile(lcu, p["puuid"], p["cid"], queue)}
        if log:
            log(f"[scout] elo e maestria de {len(rest)} jogadores em {time.monotonic() - t0:.1f} s")

    with ThreadPoolExecutor(max_workers=2) as ex:
        jobs = [ex.submit(quick)] + ([ex.submit(jungler)] if ej else [])
        for j in jobs:
            j.result()
    done = 0
    for p in rest:
        if p["cid"] not in hists:
            if time.monotonic() > deadline - 7:      # não começa um histórico que não termina a tempo
                out["partial"] = True
                continue
            try:
                hists[p["cid"]] = history(lcu, p["puuid"])
            except LCUError:
                out["partial"] = True
                continue
        out["players"][p["cid"]].update(history_profile(hists[p["cid"]], p["cid"]))
        done += 1
    for pf in out["players"].values():
        pf["alerts"] = alerts(pf, pf.get("role"))
    out["elapsed"] = round(time.monotonic() - t0, 1)
    out["withHistory"] = done
    if log:
        log(f"[scout] fim em {out['elapsed']} s: histórico de {done}/{len(rest)} jogadores")
    return out


def jungler_lines(j: dict, champ_name: str) -> list[str]:
    """Texto curto do pathing pessoal do jungler (para terminal e página)."""
    if not j or j.get("error"):
        return [f"{champ_name}: sem histórico do jogador ({(j or {}).get('error', 'não encontrado')})"]
    n = j.get("analyzed", 0)
    pool = "ranqueadas (Riot API)" if j.get("source") == "riot" else "no Summoner's Rift (cliente)"
    head = (f"{champ_name} (o jogador): jungle em {j['jungleGames']} das últimas {j['recentSR']} {pool}; "
            f"{j['withChamp']} delas com {champ_name}")
    if not n:
        return [head + "; nenhuma partida de jungle para analisar"]
    pat = j["pattern"]
    from winprob import _fmt_pct
    ft = pat["firstTakedown"]
    return [head,
            f"nas últimas {n} partidas de jungle: começa em {_fmt_pct(pat['startCamp'], 2)}; "
            f"no minuto 3 está em {_fmt_pct(pat['byMinute']['3']['side'], 2)}",
            f"1ª participação em abate: mediana {ft['medianTime'] or '-'}, antes de 4:00 em {ft['before4min'] * 100:.0f}%, "
            f"lado {_fmt_pct(ft['side'], 2) or '-'}"]


def mmss_or(t):
    return mmss(t) if t else "-"
