"""Pré-jogo: com os 10 campeões da seleção, diz para que lado começar e o que esperar nos primeiros minutos.

  lol pregame                                   lê a seleção de campeões do cliente (só GET) e imprime o plano
  lol pregame --wait [--timeout 900]            espera a seleção ter os campeões inimigos e imprime uma vez
  lol pregame --ally "top=Darius,jungle=Cho'Gath,mid=Ahri,bot=Jinx,sup=Thresh" --enemy "Shaco,Akali,Yorick,Tristana,Lulu"
              [--side blue|red] [--me "Cho'Gath"] [--json]

Com um print da seleção: identifique os campeões na imagem e use --ally/--enemy (roles dos inimigos são inferidas
pelas partidas coletadas). Nomes ignoram maiúsculas e pontuação (chogath == Cho'Gath).

De onde vem cada coisa (tudo é TENDÊNCIA estimada, não previsão do que esse jogador vai fazer):
  - jungle inimigo: data/winprob/jungle_patterns.json (`lol junglers`): campo inicial, lado nos minutos 2-3,
    primeira participação em abate (horário e lado), morte antes de 10:00, primeiro objetivo;
  - força de início de cada campeão na role: ouro do time aos 10:00 e vitória nas partidas coletadas
    (data/winprob/samples.jsonl), puxados para zero quando a amostra é pequena (n/(n+15));
  - seu histórico: data/summaries (lado em que você estava nos minutos 2-4 e resultado; partidas com o campeão);
  - horários de dragão e vastilarvas: inferidos do seu histórico (`lol timers`).
Não considera composição, itens, runas, elo de cada jogador nem duplas: com ~1.200 partidas não há amostra para isso.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from collections import Counter, defaultdict

from common import DATA, LCU, LCUError, _norm, load_json, mmss, resolve_player

WP = DATA / "winprob"
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_PT = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "atirador", "UTILITY": "suporte"}
ROLE_IN = {"top": "TOP", "jungle": "JUNGLE", "jg": "JUNGLE", "jungler": "JUNGLE", "mid": "MIDDLE", "middle": "MIDDLE",
           "bot": "BOTTOM", "bottom": "BOTTOM", "adc": "BOTTOM", "atirador": "BOTTOM", "sup": "UTILITY",
           "support": "UTILITY", "suporte": "UTILITY", "utility": "UTILITY"}
SHRINK = 15          # partidas "fantasmas" com média zero (força) ou 50% (vitória)
CALL = 150           # diferença de força (ouro do time aos 10:00) a partir da qual um lado é "mais forte"
SIDE_GAP = 0.15      # diferença de probabilidade entre lados para dizer que o jungle inimigo "costuma ir" a um lado
# lado do mapa (top = metade do barão/vastilarvas; bot = metade do dragão) -> campos de cada time nessa metade
CAMPS = {("top", 100): "blue buff, aves ou lobos", ("bot", 100): "red buff, aves-galinhas (raptors) ou krugs",
         ("top", 200): "red buff, aves-galinhas (raptors) ou krugs", ("bot", 200): "blue buff, aves ou lobos"}


# ============================================================================ campeões
def champions() -> dict[int, str]:
    p = DATA / "static" / "champions.json"
    # ids >= 10000 são variantes de modos especiais (ex.: 60031 "Jade_Chogath") com o mesmo nome
    return {int(c["id"]): c["name"] for c in load_json(p) if 0 < int(c["id"]) < 10000} if p.exists() else {}


def champ_id(name: str, champs: dict[int, str]) -> int:
    n = _norm(name)
    hit = [cid for cid, nm in champs.items() if _norm(nm) == n]
    if not hit:  # prefixo único (ex.: "cho" -> Cho'Gath)
        hit = [cid for cid, nm in champs.items() if _norm(nm).startswith(n)]
    if len(hit) != 1:
        raise SystemExit(f"Campeão não reconhecido: '{name}'" + (f" (ambíguo: {[champs[c] for c in hit]})" if hit else
                         " (rode `lol static` com o cliente aberto se a lista de campeões estiver vazia)"))
    return hit[0]


def parse_team(parts: list[str] | str, champs: dict[int, str]) -> list[tuple[str | None, int]]:
    """Aceita "top=Darius,jungle=Cho'Gath,..." ou os mesmos itens em argumentos soltos ("top Darius jungle chogath"):
    o cmd.exe do Windows quebra vírgulas e "=" quando o PowerShell passa o texto sem aspas. Uma role vale para o
    campeão seguinte; nomes com espaço que chegaram partidos ("Master" "Yi") são juntados."""
    if isinstance(parts, str):
        parts = [parts]
    toks = [t.strip() for p in parts for t in p.replace("=", ",").replace(";", ",").split(",") if t.strip()]
    out, role, i = [], None, 0
    while i < len(toks):
        t = toks[i]
        if t.lower() in ROLE_IN:
            role = ROLE_IN[t.lower()]
            i += 1
            continue
        cid = None
        for j in range(min(len(toks), i + 3), i, -1):   # tenta o nome mais longo primeiro (até 3 palavras)
            try:
                cid = champ_id(" ".join(toks[i:j]), champs)
                i = j
                break
            except SystemExit:
                if j == i + 1:
                    raise
        out.append((role, cid))
        role = None
    if len(out) > 5:
        raise SystemExit(f"Mais de 5 campeões em um time: {[champs[c] for _, c in out]}")
    return out


# ============================================================================ dados coletados
def load_samples() -> list[dict]:
    p = WP / "samples.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def role_stats(samples: list[dict]) -> tuple[dict, dict]:
    """(contagem campeão->role, força de início por (campeão, role))."""
    roles = defaultdict(Counter)
    acc = defaultdict(lambda: [0, 0.0, 0])   # n, soma ouro@10 do time (perspectiva do campeão), vitórias
    for r in samples:
        fr = r.get("frames") or []
        g10 = None
        if len(fr) > 10:
            g10 = fr[10][1] - fr[10][5]          # azul - vermelho aos 10:00
        for t, by in r["champs"].items():
            for role, (cid, _nm) in by.items():
                roles[cid][role] += 1
                if g10 is None:
                    continue
                a = acc[(cid, role)]
                a[0] += 1
                a[1] += g10 if t == "100" else -g10
                a[2] += int(r["win"] == int(t))
    strength = {}
    for k, (n, s, w) in acc.items():
        strength[k] = {"n": n, "gold10": round(s / (n + SHRINK)), "gold10Raw": round(s / n) if n else None,
                       "winRate": round((w + SHRINK / 2) / (n + SHRINK), 3), "winRateRaw": round(w / n, 3) if n else None}
    return roles, strength


def assign_roles(cids: list[int], roles: dict, fixed: dict[int, str] | None = None) -> dict[int, str]:
    """Atribuição de roles mais provável (P(role|campeão) pelas partidas coletadas)."""
    fixed = dict(fixed or {})
    free = [c for c in cids if c not in fixed]
    left = [r for r in ROLES if r not in fixed.values()]

    def lp(c, r):
        cnt = roles.get(c, Counter())
        return math.log((cnt[r] + 0.5) / (sum(cnt.values()) + 2.5))
    best, best_s = None, -1e18
    for perm in itertools.permutations(left, len(free)):
        sc = sum(lp(c, r) for c, r in zip(free, perm))
        if sc > best_s:
            best, best_s = perm, sc
    fixed.update(dict(zip(free, best or ())))
    return fixed


def role_confidence(cid: int, role: str, roles: dict) -> tuple[float, int]:
    cnt = roles.get(cid, Counter())
    n = sum(cnt.values())
    return (cnt[role] / n if n else 0.0), n


# ============================================================================ seu histórico
def my_history(my_cid: int | None, enemy_jg: int | None, champs: dict[int, str]) -> dict:
    from summarize import cached_ids, get_summary
    from zones import zone_abs
    out = {"games": 0, "bySide": {}, "withChamp": None, "vsJungler": None}
    side_res = defaultdict(lambda: [0, 0])
    wc = [0, 0]
    vj = [0, 0]
    for g in cached_ids():
        s = get_summary(g)
        if s["mapId"] != 11 or (s["duration"] or 0) < 600:
            continue
        p = resolve_player(s["participants"], "me")
        if not p:
            continue
        out["games"] += 1
        win = bool(p["stats"]["win"])
        if p.get("role") == "JUNGLE":
            cnt = Counter()
            for r in s["perMinute"]:
                if 2 <= r["minute"] <= 4:
                    v = r["players"][str(p["pid"])]
                    if v["x"] is None:
                        continue
                    z = zone_abs(v["x"], v["y"])
                    cnt["base" if z.startswith("base") else "top" if v["y"] - v["x"] > 1200
                        else "bot" if v["y"] - v["x"] < -1200 else "centro"] += 1
            side = "bot" if cnt["bot"] > cnt["top"] else "top" if cnt["top"] > cnt["bot"] else None
            if side:
                side_res[side][0] += 1
                side_res[side][1] += win
        if my_cid and p.get("championId") == my_cid:
            wc[0] += 1
            wc[1] += win
        if enemy_jg:
            ej = next((q for q in s["participants"] if q["team"] != p["team"] and q.get("role") == "JUNGLE"), None)
            if ej and ej.get("championId") == enemy_jg:
                vj[0] += 1
                vj[1] += win
    out["bySide"] = {k: {"games": n, "wins": w} for k, (n, w) in side_res.items()}
    if my_cid:
        out["withChamp"] = {"champion": champs.get(my_cid), "games": wc[0], "wins": wc[1]}
    if enemy_jg:
        out["vsJungler"] = {"champion": champs.get(enemy_jg), "games": vj[0], "wins": vj[1]}
    return out


def my_main_role() -> str:
    """Sua role mais comum nas partidas em cache (padrão para o seu campeão quando o print não diz a role)."""
    from summarize import cached_ids, get_summary
    c = Counter()
    for g in cached_ids():
        s = get_summary(g)
        p = resolve_player(s["participants"], "me")
        if p and p.get("role") in ROLES:
            c[p["role"]] += 1
    return c.most_common(1)[0][0] if c else "JUNGLE"


def timers_summary() -> dict:
    try:
        from decisions import infer_timers
        from summarize import cached_ids, get_summary
        t = infer_timers([get_summary(g) for g in cached_ids()])
        return {"dragão": t.get("DRAGON", {}).get("first"), "vastilarvas": t.get("HORDE", {}).get("first")}
    except Exception:
        return {}


def base_chance(team: int | None) -> float | None:
    if not team:
        return None
    try:
        from winprob import MODEL, _state, win_prob
        if not MODEL.exists():
            return None
        st = _state([[0, 2500, 0, 0, 5, 2500, 0, 0, 5]], [], 0)
        return win_prob(st, team)
    except (Exception, SystemExit):
        return None


# ============================================================================ plano
def side_shares(d: dict) -> tuple[float, float]:
    return d.get("top", 0.0), d.get("bot", 0.0)


def plan(ally: dict[str, int], enemy: dict[str, int], team: int | None, me_role: str, champs: dict[int, str]) -> dict:
    samples = load_samples()
    roles, strength = role_stats(samples)
    pats = {}
    pp = WP / "jungle_patterns.json"
    if pp.exists():
        pats = load_json(pp).get("champions", {})

    def st(cid, role):
        s = strength.get((cid, role))
        return s or {"n": 0, "gold10": 0, "gold10Raw": None, "winRate": 0.5, "winRateRaw": None}

    lanes = {}
    for side, rs in (("top", ["TOP"]), ("mid", ["MIDDLE"]), ("bot", ["BOTTOM", "UTILITY"])):
        a = sum(st(ally[r], r)["gold10"] for r in rs if r in ally)
        e = sum(st(enemy[r], r)["gold10"] for r in rs if r in enemy)
        lanes[side] = {"ally": [{"role": r, "champion": champs.get(ally[r]), **st(ally[r], r)} for r in rs if r in ally],
                       "enemy": [{"role": r, "champion": champs.get(enemy[r]), **st(enemy[r], r)} for r in rs if r in enemy],
                       "edge": a - e}

    ej_cid = enemy.get("JUNGLE")
    ej_name = champs.get(ej_cid) if ej_cid else None
    pat = pats.get(ej_name) if ej_name else None
    my_cid = ally.get(me_role)
    hist = my_history(my_cid, ej_cid, champs)
    timers = timers_summary()
    out = {"team": team, "meRole": me_role, "me": champs.get(my_cid), "ally": {r: champs.get(c) for r, c in ally.items()},
           "enemy": {r: champs.get(c) for r, c in enemy.items()},
           "enemyRoleConfidence": {r: role_confidence(c, r, roles)[0] for r, c in enemy.items()},
           "samples": len(samples), "lanes": lanes, "enemyJungle": None, "history": hist, "timers": timers,
           "baseChance": base_chance(team), "reasons": [], "recommendation": None, "confidence": "baixa", "expect": []}

    # ---------------- jungle inimigo
    ej_side, ej_info = None, None
    if pat:
        ft = pat["firstTakedown"]
        t3, b3 = side_shares(pat["byMinute"].get("3", {}).get("side", {}))
        tf, bf = side_shares(ft.get("side", {}))
        # lado em que ele aparece no primeiro abate pesa mais (é onde ele ganka); minuto 3 confirma
        top_p, bot_p = 0.6 * tf + 0.4 * t3, 0.6 * bf + 0.4 * b3
        if top_p - bot_p >= SIDE_GAP:
            ej_side = "top"
        elif bot_p - top_p >= SIDE_GAP:
            ej_side = "bot"
        ej_info = {"champion": ej_name, "games": pat["games"], "startCamp": pat["startCamp"],
                   "minute3Side": pat["byMinute"].get("3", {}).get("side", {}),
                   "firstTakedown": ft, "diesBefore10": pat["firstDeathBefore10"],
                   "firstObjective": pat["firstTeamObjective"], "likelySide": ej_side,
                   "sideScore": {"top": round(top_p, 2), "bot": round(bot_p, 2)}}
        out["enemyJungle"] = ej_info

    # ---------------- recomendação (só faz sentido para quem joga de jungle)
    edge_top, edge_bot = lanes["top"]["edge"], lanes["bot"]["edge"]
    strong = "top" if edge_top - edge_bot >= CALL else "bot" if edge_bot - edge_top >= CALL else None
    R = out["reasons"]
    rec = None
    if ej_side and pat:
        other = "bot" if ej_side == "top" else "top"
        ft = pat["firstTakedown"]
        when = ft.get("medianTime") or "?"
        R.append(f"{ej_name} costuma aparecer primeiro no lado {ej_side} ({round(100 * ej_info['sideScore'][ej_side])}% "
                 f"× {round(100 * ej_info['sideScore'][other])}%; {pat['games']} partidas no elo), primeiro abate "
                 f"com mediana {when}")
        if lanes[ej_side]["edge"] >= CALL or strong == ej_side:
            rec = ej_side
            R.append(f"o lado {ej_side} é o seu melhor começo (saldo estimado aos 10:00: {fmt(lanes[ej_side]['edge'])}): "
                     f"esteja lá para o contra-gank")
        else:
            rec = other
            R.append(f"o lado {ej_side} não é o seu melhor começo (saldo estimado aos 10:00: {fmt(lanes[ej_side]['edge'])}): "
                     f"avise essa lane para jogar seguro até ~{when} e troque do lado {other}")
        out["confidence"] = "média" if pat["games"] >= 30 else "baixa"
    elif strong:
        rec = strong
        weak = "bot" if strong == "top" else "top"
        R.append(f"sem lado claro para o jungle inimigo; o lado {strong} começa melhor que o {weak} "
                 f"(saldo estimado aos 10:00: {fmt(lanes[strong]['edge'])} × {fmt(lanes[weak]['edge'])})")
    else:
        bs = hist["bySide"]
        t, b = bs.get("top", {"games": 0, "wins": 0}), bs.get("bot", {"games": 0, "wins": 0})
        if t["games"] >= 10 and b["games"] >= 10:
            wt, wb = t["wins"] / t["games"], b["wins"] / b["games"]
            if abs(wt - wb) >= 0.10:
                rec = "top" if wt > wb else "bot"
                R.append(f"sem sinal no draft; no seu histórico você ganha mais começando no lado {rec} "
                         f"(top {t['wins']}/{t['games']}, bot {b['wins']}/{b['games']})")
    if rec is None:
        R.append("sem sinal forte no draft nem no seu histórico: siga o primeiro objetivo "
                 f"(dragão a partir de ~{mmss(timers['dragão']) if timers.get('dragão') else '5:00'} no lado bot, "
                 f"vastilarvas a partir de ~{mmss(timers['vastilarvas']) if timers.get('vastilarvas') else '8:00'} no lado top)")
    out["recommendation"] = rec
    if rec and team:
        out["startCamps"] = CAMPS[(rec, team)]
    if me_role != "JUNGLE":
        R.append(f"você está de {ROLE_PT.get(me_role, me_role)}: o lado é a decisão do seu jungler; use o resto como aviso")

    # ---------------- o que esperar
    E = out["expect"]
    if pat:
        ft = pat["firstTakedown"]
        E.append(f"{ej_name}: começa em {top(pat['startCamp'], 2)}; no minuto 3 está em {top(pat['byMinute'].get('3', {}).get('side', {}), 2)}")
        if ft.get("medianTime"):
            E.append(f"{ej_name}: primeira participação em abate com mediana {ft['medianTime']}, antes de 4:00 em "
                     f"{pct(ft['before4min'])} das partidas, nas lanes {top(ft.get('zone', {}), 3)}")
        E.append(f"{ej_name}: morre antes de 10:00 em {pct(pat['firstDeathBefore10'])} das partidas; primeiro objetivo "
                 f"do time dele até 10:00: {top(pat['firstTeamObjective'], 3)}")
    elif ej_name:
        E.append(f"{ej_name}: sem amostra no jungle (menos de 5 partidas coletadas): sem padrão para esperar")
    for side in ("top", "mid", "bot"):
        L = lanes[side]
        if not L["ally"] or not L["enemy"]:
            continue
        n_min = min(x["n"] for x in L["ally"] + L["enemy"])
        tag = "amostra pequena" if n_min < 15 else f"n ≥ {n_min}"
        E.append(f"lane {side}: {', '.join(x['champion'] for x in L['ally'])} × {', '.join(x['champion'] for x in L['enemy'])}: "
                 f"saldo estimado aos 10:00 para o seu time {fmt(L['edge'])} ({tag})")
    if timers.get("dragão"):
        E.append(f"dragão nasce até ~{mmss(timers['dragão'])} (lado bot); vastilarvas até ~{mmss(timers.get('vastilarvas') or 0)} (lado top)")
    return out


def fmt(v: float) -> str:
    return (f"{v:+,.0f}".replace(",", ".").replace("-", "−"))


def pct(x: float) -> str:
    return f"{round(100 * x)}%"


def top(d: dict, k: int = 2) -> str:
    return ", ".join(f"{a} {pct(v)}" for a, v in list(d.items())[:k]) or "?"


# ============================================================================ saída
def print_plan(p: dict) -> None:
    team = {100: "time azul", 200: "time vermelho"}.get(p["team"], "lado do mapa não informado (--side blue|red)")
    print(f"Pré-jogo · você: {p['me'] or '?'} ({ROLE_PT.get(p['meRole'], p['meRole'])}) · {team}")
    print("Seu time: " + ", ".join(f"{ROLE_PT[r]} {p['ally'][r]}" for r in ROLES if r in p["ally"]))
    conf = p["enemyRoleConfidence"]
    print("Inimigo:  " + ", ".join(f"{ROLE_PT[r]} {p['enemy'][r]}" + ("?" if conf.get(r, 1) < 0.6 else "")
                                   for r in ROLES if r in p["enemy"]) + "   (? = role incerta)")
    if p["baseChance"] is not None:
        print(f"Chance de vitória no minuto 0 (modelo): {pct(p['baseChance'])}; o modelo não vê campeões, só o lado do mapa.")
    print()
    if p["recommendation"]:
        cam = f" (comece por {p['startCamps']})" if p.get("startCamps") else ""
        print(f">> Comece pelo lado {p['recommendation'].upper()}{cam}. Confiança {p['confidence']}.")
    else:
        print(">> Sem recomendação de lado: os dados não apontam um.")
    for r in p["reasons"]:
        print(f"   - {r}")
    print("\nO que esperar (tendências estimadas, não certeza):")
    for e in p["expect"]:
        print(f"   - {e}")
    h = p["history"]
    print(f"\nSeu histórico ({h['games']} partidas em cache):")
    bs = h["bySide"]
    if bs:
        print("   - de jungle, começando (min 2-4) no lado " + "; ".join(
            f"{k}: {v['wins']}/{v['games']} vitórias ({pct(v['wins'] / v['games'])})" for k, v in sorted(bs.items())))
    for key, label in (("withChamp", "com"), ("vsJungler", "contra o jungle")):
        x = h.get(key)
        if x and x["champion"]:
            print(f"   - {label} {x['champion']}: {x['wins']}/{x['games']} vitórias" if x["games"] else f"   - {label} {x['champion']}: nenhuma partida")
    print(f"\nFontes: {p['samples']} partidas coletadas (lol winprob crawl), padrões de jungle (lol junglers), seu cache. "
          "Não considera composição, itens, runas nem o elo de cada jogador. Posições só a cada minuto.")


# ============================================================================ cliente
POS_IN = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "bottom": "BOTTOM", "utility": "UTILITY"}


def read_champ_select(lcu: LCU) -> dict | None:
    """Sessão da seleção de campeões (só GET). None se o cliente não estiver na seleção."""
    phase = lcu.get("/lol-gameflow/v1/gameflow-phase")
    if phase != "ChampSelect":
        return {"phase": phase}
    ses = lcu.get("/lol-champ-select/v1/session") or {}
    me_cell = ses.get("localPlayerCellId")
    my = ses.get("myTeam") or []
    their = ses.get("theirTeam") or []
    ally, me = [], None
    for m in my:
        cid = m.get("championId") or m.get("championPickIntent") or 0
        role = POS_IN.get((m.get("assignedPosition") or "").lower())
        if cid:
            ally.append((role, int(cid)))
        if m.get("cellId") == me_cell:
            me = (role, int(cid) if cid else None)
    enemy = [(None, int(m["championId"])) for m in their if m.get("championId")]
    team_raw = (my[0].get("team") if my else None)
    team = {1: 100, 2: 200}.get(team_raw)
    return {"phase": phase, "ally": ally, "enemy": enemy, "me": me, "team": team,
            "complete": len(ally) == 5 and len(enemy) == 5}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ally", nargs="+", help='seu time: "top=Darius,jungle=Cho\'Gath,..." (roles opcionais)')
    ap.add_argument("--enemy", nargs="+", help='time inimigo: "Shaco,Akali,..." (roles opcionais, inferidas se faltarem)')
    ap.add_argument("--side", choices=["blue", "red", "azul", "vermelho"])
    ap.add_argument("--me", help="seu campeão (padrão: o jungler do seu time)")
    ap.add_argument("--wait", action="store_true", help="espera a seleção de campeões ter os 10 campeões")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    champs = champions()
    if not champs:
        raise SystemExit("Lista de campeões vazia: rode `lol static` com o cliente aberto.")
    samples_ok = (WP / "samples.jsonl").exists()
    if not samples_ok:
        print("[aviso] sem data/winprob/samples.jsonl: rode `lol winprob crawl` para ter força de lane e padrões de jungle.",
              file=sys.stderr)

    team, me_role, me_cid = None, "JUNGLE", None
    if a.ally or a.enemy:
        if not (a.ally and a.enemy):
            raise SystemExit("Informe --ally e --enemy (5 campeões cada; roles opcionais).")
        ally_l, enemy_l = parse_team(a.ally, champs), parse_team(a.enemy, champs)
        team = {"blue": 100, "azul": 100, "red": 200, "vermelho": 200}.get(a.side or "")
        main_role = my_main_role()
        if a.me:
            me_cid = champ_id(a.me, champs)
            if me_cid not in [c for _, c in ally_l]:
                raise SystemExit(f"--me {a.me}: esse campeão não está em --ally.")
            if not any(c == me_cid and r for r, c in ally_l) and main_role not in [r for r, _ in ally_l if r]:
                ally_l = [(main_role if c == me_cid else r, c) for r, c in ally_l]
        me_role = next((r for r, c in ally_l if c == me_cid and r), main_role)
    else:
        try:
            lcu = LCU()
            t0 = time.time()
            while True:
                cs = read_champ_select(lcu)
                if cs.get("phase") == "ChampSelect" and (cs["complete"] or not a.wait):
                    break
                if not a.wait:
                    raise SystemExit(f"O cliente não está na seleção de campeões (fase: {cs.get('phase')}). "
                                     "Use --wait para esperar, ou --ally/--enemy com os campeões de um print.")
                if time.time() - t0 > a.timeout:
                    raise SystemExit("Tempo esgotado esperando a seleção de campeões.")
                time.sleep(3)
        except LCUError as e:
            raise SystemExit(f"Cliente indisponível: {e}") from None
        ally_l, enemy_l, team = cs["ally"], cs["enemy"], cs["team"]
        if cs["me"]:
            me_role = cs["me"][0] or me_role
            me_cid = cs["me"][1]
        if len(enemy_l) < 5:
            print(f"[aviso] só {len(enemy_l)} campeão(ões) inimigo(s) visível(is) até agora.", file=sys.stderr)

    roles, _ = role_stats(load_samples())
    ally = assign_roles([c for _, c in ally_l], roles, {c: r for r, c in ally_l if r})
    enemy = assign_roles([c for _, c in enemy_l], roles, {c: r for r, c in enemy_l if r})
    ally_by_role = {r: c for c, r in ally.items()}
    enemy_by_role = {r: c for c, r in enemy.items()}
    if me_cid and me_cid in ally:
        me_role = ally[me_cid]
    p = plan(ally_by_role, enemy_by_role, team, me_role, champs)
    if a.json:
        print(json.dumps(p, ensure_ascii=False))
    else:
        print_plan(p)


if __name__ == "__main__":
    main()
