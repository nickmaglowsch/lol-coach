"""Pré-jogo: com os 10 campeões da seleção, diz para que lado começar e o que esperar nos primeiros minutos.

  lol pregame                                   lê o jogo atual (só GET) e imprime o plano; a fonte é escolhida sozinha:
                                                  jogo aberto (Live Client Data, porta 2999) > carregamento
                                                  (/lol-gameflow/v1/session) > seleção de campeões (/lol-champ-select/v1/session)
  lol pregame --wait [--timeout 900]            espera ter os 10 campeões (de qualquer fonte) e imprime uma vez
  lol pregame --source selecao|carregamento|jogo  força uma fonte (para testar)
  lol pregame --ally "top=Darius,jungle=Cho'Gath,mid=Ahri,bot=Jinx,sup=Thresh" --enemy "Shaco,Akali,Yorick,Tristana,Lulu"
              [--side blue|red] [--me "Cho'Gath"] [--json]

A resposta bruta de cada fonte fica em data/pregame/ultima-<fonte>.json (fora do git), para conferir o formato.
No carregamento e no jogo aberto, quem leva Smite é o jungler (sem chute). Na seleção, só o seu time tem roles.
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

from pathlib import Path

from common import (DATA, LCU, LCUError, REPORTS, LiveClientError, _norm, live_client_get, load_json, me_ids, mmss,
                    resolve_player, save_json)

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
# (nomes e posições de tools/landmarks.py: aves azul (6943, 5422) fica na metade bot; aves vermelho na metade top)
CAMPS = {("top", 100): "blue buff, gromp ou lobos", ("bot", 100): "red buff, aves ou krugs",
         ("top", 200): "red buff, aves ou krugs", ("bot", 200): "blue buff, gromp ou lobos"}


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
    print(f"Pré-jogo · você: {p['me'] or '?'} ({ROLE_PT.get(p['meRole'], p['meRole'])}) · {team} · fonte: {p.get('source', '?')}")
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


# ============================================================================ página (artifact)
TEMPLATE = Path(__file__).resolve().parent / "templates" / "pregame.html"
# centro aproximado da jungle de cada time em cada metade, no viewBox 0-100 (azul embaixo à esquerda)
JG_SPOT = {(100, "top"): (26, 46), (100, "bot"): (53, 73), (200, "top"): (48, 27), (200, "bot"): (74, 53)}


def map_svg(rec: str | None, team: int | None, ej_side: str | None, ej_label: str) -> str:
    def half(side):
        pts = "0,0 100,0 0,100" if side == "top" else "100,0 100,100 0,100"
        fill = "var(--map-pick)" if rec == side else "var(--map)"
        return f'<polygon points="{pts}" fill="{fill}"/>'
    my = {100: "var(--ally)", 200: "var(--enemy)"}
    blue, red = (my[100], my[200]) if team != 200 else (my[200], my[100])
    out = [f'<svg viewBox="0 0 100 100" role="img" aria-label="Mapa: lado recomendado {rec or "nenhum"}">',
           '<rect x="0" y="0" width="100" height="100" rx="6" fill="var(--map)"/>', half("top"), half("bot"),
           '<line x1="2" y1="2" x2="98" y2="98" stroke="var(--river)" stroke-width="7" stroke-linecap="round"/>',
           '<path d="M8 92 L8 8 L92 8 M8 92 L92 92 L92 8 M8 92 L92 8" fill="none" stroke="var(--muted)" '
           'stroke-opacity=".45" stroke-width="2"/>',
           f'<circle cx="8" cy="92" r="6" fill="{blue}"/><circle cx="92" cy="8" r="6" fill="{red}"/>',
           '<text x="31" y="27" font-size="7" fill="var(--muted)" text-anchor="middle">barão</text>',
           '<text x="70" y="78" font-size="7" fill="var(--muted)" text-anchor="middle">dragão</text>']
    if ej_side:
        enemy_team = 200 if team != 200 else 100
        x, y = JG_SPOT[(enemy_team, ej_side)] if team else ((30, 30) if ej_side == "top" else (70, 70))
        out.append(f'<circle cx="{x}" cy="{y}" r="4.5" fill="var(--enemy)" stroke="var(--surface)" stroke-width="1.5"/>'
                   f'<text x="{x}" y="{y - 7}" font-size="7" font-weight="600" fill="var(--enemy)" text-anchor="middle" '
                   f'paint-order="stroke" stroke="var(--surface)" stroke-width="2.5">{H(ej_label)}</text>')
    out.append("</svg>")
    return "".join(out)


def H(x) -> str:
    import html
    return html.escape(str(x))


def page(p: dict, alias: str) -> str:
    import datetime as dt
    rec, ej = p["recommendation"], p.get("enemyJungle")
    team_txt = {100: "time azul", 200: "time vermelho"}.get(p["team"], "lado do mapa não informado")
    ej_label = f'{ej["champion"]} ~{ej["firstTakedown"].get("medianTime") or "?"}' if ej else ""
    B = []
    head = f'Pré-jogo · {H(p["me"] or "?")} ({ROLE_PT.get(p["meRole"], p["meRole"])}) · {team_txt}'
    if rec:
        title = f"Comece pelo {rec}"
        camps = f'<p class="camps">{H(p["startCamps"])}</p>' if p.get("startCamps") else \
            '<p class="sub">informe o lado do mapa para ver os campos</p>'
        conf = f'<span class="conf {"media" if p["confidence"] == "média" else ""}">confiança {H(p["confidence"])}</span>'
    else:
        title, camps, conf = "Sem lado definido", '<p class="sub">os dados não apontam um lado</p>', ""
    B.append(f'<section class="pick">{map_svg(rec, p["team"], ej and ej.get("likelySide"), ej_label)}'
             f'<div class="txt"><div class="eyebrow">{head}</div><h1>{H(title)}</h1>{camps}{conf}</div></section>')
    B.append('<ul class="why">' + "".join(f"<li>{H(r[:1].upper() + r[1:])}</li>" for r in p["reasons"]) + "</ul>")

    if ej:
        ft = ej["firstTakedown"]
        side = ej.get("likelySide")
        side_txt = (f'{side} {pct(ej["sideScore"][side])}' if side else
                    f'top {pct(ej["sideScore"]["top"])} · bot {pct(ej["sideScore"]["bot"])}')
        cells = [(ft.get("medianTime") or "—", "1º abate (mediana)"),
                 (pct(ft["before4min"]), "abate antes de 4:00"),
                 (side_txt, "lado provável" if side else "sem lado claro"),
                 (pct(ej["diesBefore10"]), "morre antes de 10:00")]
        kv = "".join(f"<div><b>{H(v)}</b><span>{H(k)}</span></div>" for v, k in cells)
        B.append(f'<section><div class="eyebrow">Jungle inimigo</div><h2>{H(ej["champion"])} '
                 f'<span class="sub">· {ej["games"]} partidas no elo</span></h2><div class="kv">{kv}</div>'
                 f'<p class="sub">Começa em {H(top(ej["startCamp"], 2))}. Primeiro objetivo do time dele até 10:00: '
                 f'{H(top(ej["firstObjective"], 3))}.</p></section>')
    elif p["enemy"].get("JUNGLE"):
        B.append(f'<section><div class="eyebrow">Jungle inimigo</div><h2>{H(p["enemy"]["JUNGLE"])}</h2>'
                 '<p class="sub">Menos de 5 partidas desse campeão no jungle entre as coletadas: sem padrão.</p></section>')

    rows = []
    for side, lab in (("top", "TOP"), ("mid", "MID"), ("bot", "BOT")):
        L = p["lanes"][side]
        if not L["ally"] or not L["enemy"]:
            continue
        n_min = min(x["n"] for x in L["ally"] + L["enemy"])
        cls = "pos" if L["edge"] >= 100 else "neg" if L["edge"] <= -100 else "zero"
        who = (f'<span class="ally">{H(" + ".join(x["champion"] for x in L["ally"]))}</span> × '
               f'<span class="enemy">{H(" + ".join(x["champion"] for x in L["enemy"]))}</span>')
        rows.append(f'<div class="lane"><span class="eyebrow">{lab}</span><span class="who">{who}</span>'
                    f'<span class="val {cls}">{fmt(L["edge"])}<small>{"amostra pequena" if n_min < 15 else f"n ≥ {n_min}"}</small></span></div>')
    if rows:
        B.append('<section><div class="eyebrow">Lanes</div><h2>Saldo estimado do time aos 10:00</h2>'
                 f'<div class="lanes">{"".join(rows)}</div></section>')

    def roster(d, conf=None):
        return "<ul>" + "".join(f'<li><span>{ROLE_PT[r]}</span>{H(d[r])}{"?" if conf and conf.get(r, 1) < 0.6 else ""}</li>'
                                for r in ROLES if r in d) + "</ul>"
    B.append(f'<section class="teams"><div><div class="eyebrow ally">Seu time</div>{roster(p["ally"])}</div>'
             f'<div><div class="eyebrow enemy">Inimigo</div>{roster(p["enemy"], p["enemyRoleConfidence"])}</div></section>')

    h = p["history"]
    li = []
    for k, v in sorted(h["bySide"].items()):
        li.append(f'De jungle, começando no lado {k}: {v["wins"]}/{v["games"]} vitórias ({pct(v["wins"] / v["games"])})')
    for key, label in (("withChamp", "Com"), ("vsJungler", "Contra o jungle")):
        x = h.get(key)
        if x and x["champion"]:
            li.append(f'{label} {x["champion"]}: {x["wins"]}/{x["games"]} vitórias' if x["games"] else f'{label} {x["champion"]}: nenhuma partida')
    if li:
        B.append(f'<section><div class="eyebrow">Seu histórico · {h["games"]} partidas</div>'
                 '<ul class="hist">' + "".join(f"<li>{H(x)}</li>" for x in li) + "</ul></section>")

    base = f' Chance no minuto 0 (modelo, só pelo lado do mapa): {pct(p["baseChance"])}.' if p.get("baseChance") is not None else ""
    if p.get("timers", {}).get("dragão"):
        base += f' Dragão até ~{mmss(p["timers"]["dragão"])}, vastilarvas até ~{mmss(p["timers"].get("vastilarvas") or 0)}.'
    B.append(f'<footer>Tendências de {p["samples"]} partidas do elo e do seu histórico, não previsão do que esse jogador vai '
             f'fazer. Não considera composição, itens nem runas.{H(base)} Fonte: {H(p.get("source", "?"))}. Gerado em '
             f'{dt.datetime.now():%d/%m %H:%M} por <span class="mono">lol pregame</span>.</footer>')
    tpl = TEMPLATE.read_text(encoding="utf-8")
    return tpl.replace("{{TITLE}}", H(f"Pré-jogo · {alias}")).replace("{{BODY}}", "\n".join(B))


def write_page(p: dict, path: Path) -> Path:
    me = DATA / "me.json"
    alias = (load_json(me).get("alias") if me.exists() else None) or "você"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page(p, alias), encoding="utf-8")
    return path


# ============================================================================ jogo atual (só leitura)
POS_IN = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "mid": "MIDDLE", "bottom": "BOTTOM", "bot": "BOTTOM",
          "utility": "UTILITY", "support": "UTILITY"}
SMITE_IDS = {11}           # feitiço "Golpear" (Smite)
INGAME_PHASES = {"GameStart", "InProgress", "Reconnect"}
RAW = DATA / "pregame"


def _pos(v) -> str | None:
    return POS_IN.get(str(v or "").strip().lower())


def _with_smite_as_jungle(team: list[tuple[str | None, int]], smite: set[int]) -> tuple[list, bool]:
    """Quem leva Smite é o jungler (se for exatamente um no time e a role não vier preenchida)."""
    js = [c for _, c in team if c in smite]
    if len(js) != 1 or any(r == "JUNGLE" for r, _ in team):
        return team, False
    return [("JUNGLE" if c == js[0] else (r if r != "JUNGLE" else None), c) for r, c in team], True


def parse_champ_select(ses: dict) -> dict:
    """GET /lol-champ-select/v1/session. Inimigos sem role (o cliente não mostra); feitiços só do seu time."""
    me_cell = ses.get("localPlayerCellId")
    my, their = ses.get("myTeam") or [], ses.get("theirTeam") or []
    ally, me, smite = [], None, set()
    for m in my:
        cid = int(m.get("championId") or m.get("championPickIntent") or 0)
        role = _pos(m.get("assignedPosition"))
        if cid:
            ally.append((role, cid))
            if {m.get("spell1Id"), m.get("spell2Id")} & SMITE_IDS:
                smite.add(cid)
        if m.get("cellId") == me_cell:
            me = cid or None
    ally, _ = _with_smite_as_jungle(ally, smite)
    enemy = [(None, int(m["championId"])) for m in their if m.get("championId")]
    team = {1: 100, 2: 200}.get(my[0].get("team")) if my else None
    return {"source": "seleção de campeões", "ally": ally, "enemy": enemy, "me": me, "team": team,
            "smiteEnemy": False, "complete": len(ally) == 5 and len(enemy) == 5}


def parse_gameflow(ses: dict, my_ids: set[str]) -> dict:
    """GET /lol-gameflow/v1/session durante o carregamento/partida: gameData.teamOne (azul) e teamTwo (vermelho),
    e playerChampionSelections com os feitiços (Smite => jungler)."""
    gd = ses.get("gameData") or {}
    t1, t2 = gd.get("teamOne") or [], gd.get("teamTwo") or []
    spells = {}
    for pc in gd.get("playerChampionSelections") or []:
        key = pc.get("puuid") or pc.get("summonerInternalName") or pc.get("championId")
        spells[key] = {pc.get("spell1Id"), pc.get("spell2Id")}
        if pc.get("championId"):
            spells.setdefault(("cid", int(pc["championId"])), spells[key])

    def side(rows):
        out, smite = [], set()
        for m in rows:
            cid = int(m.get("championId") or 0)
            if not cid:
                continue
            out.append((_pos(m.get("selectedPosition")), cid))
            sp = spells.get(m.get("puuid")) or spells.get(m.get("summonerInternalName")) or spells.get(("cid", cid)) or set()
            if sp & SMITE_IDS:
                smite.add(cid)
        team, fixed = _with_smite_as_jungle(out, smite)
        return team, fixed

    a, fa = side(t1)
    b, fb = side(t2)
    mine_in_two = any((m.get("puuid") or "") in my_ids for m in t2 if m.get("puuid"))
    mine_in_one = any((m.get("puuid") or "") in my_ids for m in t1 if m.get("puuid"))
    if not (mine_in_one or mine_in_two):
        team = None
    else:
        team = 200 if mine_in_two else 100
    ally, enemy, fe = (b, a, fa) if team == 200 else (a, b, fb)
    me_row = next((m for m in t1 + t2 if (m.get("puuid") or "") in my_ids), None)
    return {"source": "carregamento (gameflow)", "ally": ally, "enemy": enemy,
            "me": int(me_row["championId"]) if me_row and me_row.get("championId") else None, "team": team,
            "smiteEnemy": fe, "complete": len(ally) == 5 and len(enemy) == 5, "mapId": (gd.get("queue") or {}).get("mapId")}


def parse_live(data: dict, alias_to_id: dict[str, int], champs: dict[int, str]) -> dict:
    """GET https://127.0.0.1:2999/liveclientdata/allgamedata (jogo aberto, inclusive personalizada e treino)."""
    players = data.get("allPlayers") or []
    act = data.get("activePlayer") or {}
    me_name = (act.get("riotId") or act.get("summonerName") or "").lower()

    def cid_of(pl):
        raw = str(pl.get("rawChampionName") or "")
        alias = raw.rsplit("_", 1)[-1] if raw else ""
        if alias and _norm(alias) in alias_to_id:
            return alias_to_id[_norm(alias)]
        try:
            return champ_id(pl.get("championName") or "", champs)
        except SystemExit:
            return None

    rows = {100: [], 200: []}
    smite = {100: set(), 200: set()}
    me, team = None, None
    for pl in players:
        t = {"ORDER": 100, "CHAOS": 200}.get(pl.get("team"))
        cid = cid_of(pl)
        if not t or not cid:
            continue
        rows[t].append((_pos(pl.get("position")), cid))
        sp = pl.get("summonerSpells") or {}
        raw = " ".join(str((sp.get(k) or {}).get("rawDisplayName") or "") for k in ("summonerSpellOne", "summonerSpellTwo"))
        if "Smite" in raw:
            smite[t].add(cid)
        names = {str(pl.get(k) or "").lower() for k in ("riotId", "summonerName")} - {""}
        if me_name and me_name in names:
            me, team = cid, t
    fixed = {}
    for t in (100, 200):
        rows[t], fixed[t] = _with_smite_as_jungle(rows[t], smite[t])
    other = 200 if team == 100 else 100
    ally, enemy = (rows[team], rows[other]) if team else (rows[100], rows[200])
    return {"source": "jogo aberto (porta 2999)", "ally": ally, "enemy": enemy, "me": me, "team": team,
            "smiteEnemy": fixed.get(other, False) if team else False,
            "complete": len(ally) == 5 and len(enemy) == 5, "mapId": (data.get("gameData") or {}).get("mapNumber")}


def _raw(name: str, obj) -> None:
    try:
        save_json(RAW / f"ultima-{name}.json", obj, indent=1)
    except OSError:
        pass


def read_current(source: str, champs: dict[int, str]) -> dict:
    """Lê o jogo atual pela melhor fonte disponível. Devolve {"ready": False, "why": ...} se não houver nada."""
    alias_to_id = {}
    pc = DATA / "static" / "champions.json"
    if pc.exists():
        alias_to_id = {_norm(c.get("alias") or ""): int(c["id"]) for c in load_json(pc) if 0 < int(c["id"]) < 10000}
    if source in ("auto", "jogo"):
        try:
            d = live_client_get("allgamedata")
            _raw("jogo", d)
            r = parse_live(d or {}, alias_to_id, champs)
            if r["ally"] or r["enemy"]:
                return {"ready": True, **r}
        except LiveClientError as e:
            if source == "jogo":
                return {"ready": False, "why": f"jogo aberto: {e}"}
    try:
        lcu = LCU()
        phase = lcu.get("/lol-gameflow/v1/gameflow-phase")
    except LCUError as e:
        return {"ready": False, "why": f"cliente indisponível: {e}"}
    if source in ("auto", "carregamento") and (phase in INGAME_PHASES or source == "carregamento"):
        try:
            ses = lcu.get("/lol-gameflow/v1/session") or {}
            _raw("carregamento", ses)
            r = parse_gameflow(ses, me_ids())
            if r["ally"] or r["enemy"]:
                return {"ready": True, "phase": phase, **r}
        except LCUError as e:
            if source == "carregamento":
                return {"ready": False, "why": f"carregamento: {e}"}
    if source in ("auto", "selecao") and (phase == "ChampSelect" or source == "selecao"):
        try:
            ses = lcu.get("/lol-champ-select/v1/session") or {}
            _raw("selecao", ses)
            return {"ready": True, "phase": phase, **parse_champ_select(ses)}
        except LCUError as e:
            return {"ready": False, "why": f"seleção de campeões: {e}"}
    return {"ready": False, "why": f"o cliente não está na seleção nem numa partida (fase: {phase})"}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ally", nargs="+", help='seu time: "top=Darius,jungle=Cho\'Gath,..." (roles opcionais)')
    ap.add_argument("--enemy", nargs="+", help='time inimigo: "Shaco,Akali,..." (roles opcionais, inferidas se faltarem)')
    ap.add_argument("--side", choices=["blue", "red", "azul", "vermelho"])
    ap.add_argument("--me", help="seu campeão (padrão: o da sua role mais comum)")
    ap.add_argument("--source", choices=["auto", "selecao", "carregamento", "jogo"], default="auto",
                    help="de onde ler o jogo atual (padrão: a melhor disponível)")
    ap.add_argument("--wait", action="store_true", help="espera ter os 10 campeões")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", nargs="?", const=str(REPORTS / "pregame.html"), metavar="ARQUIVO",
                    help="grava também a página para o celular (padrão reports/pregame.html; publique sempre o mesmo arquivo)")
    a = ap.parse_args()
    champs = champions()
    if not champs:
        raise SystemExit("Lista de campeões vazia: rode `lol static` com o cliente aberto.")
    if not (WP / "samples.jsonl").exists():
        print("[aviso] sem data/winprob/samples.jsonl: rode `lol winprob crawl` para ter força de lane e padrões de jungle.",
              file=sys.stderr)

    main_role = my_main_role()
    team, me_cid, source = None, None, "campeões informados"
    if a.ally or a.enemy:
        if not (a.ally and a.enemy):
            raise SystemExit("Informe --ally e --enemy (5 campeões cada; roles opcionais).")
        ally_l, enemy_l = parse_team(a.ally, champs), parse_team(a.enemy, champs)
        team = {"blue": 100, "azul": 100, "red": 200, "vermelho": 200}.get(a.side or "")
        if a.me:
            me_cid = champ_id(a.me, champs)
            if me_cid not in [c for _, c in ally_l]:
                raise SystemExit(f"--me {a.me}: esse campeão não está em --ally.")
    else:
        t0 = time.time()
        while True:
            cur = read_current(a.source, champs)
            if cur["ready"] and (cur["complete"] or not a.wait):
                break
            if not a.wait:
                raise SystemExit(f"Sem jogo atual para ler: {cur.get('why') or 'faltam campeões'}. "
                                 "Use --wait para esperar, ou --ally/--enemy com os campeões de um print.")
            if time.time() - t0 > a.timeout:
                raise SystemExit("Tempo esgotado esperando o jogo atual.")
            time.sleep(3)
        if cur.get("mapId") not in (None, 11):
            raise SystemExit(f"O pré-jogo é só para Summoner's Rift (mapa desta partida: {cur['mapId']}).")
        ally_l, enemy_l, team, me_cid, source = cur["ally"], cur["enemy"], cur["team"], cur["me"], cur["source"]
        if a.side:
            team = {"blue": 100, "azul": 100, "red": 200, "vermelho": 200}[a.side]
        if not cur["complete"]:
            print(f"[aviso] só {len(ally_l)} aliado(s) e {len(enemy_l)} inimigo(s) até agora.", file=sys.stderr)
    # o seu campeão sem role: vai na sua role mais comum, se ela estiver livre
    if me_cid and not any(c == me_cid and r for r, c in ally_l) and main_role not in [r for r, _ in ally_l if r]:
        ally_l = [(main_role if c == me_cid else r, c) for r, c in ally_l]

    roles, _ = role_stats(load_samples())
    ally = assign_roles([c for _, c in ally_l], roles, {c: r for r, c in ally_l if r})
    enemy = assign_roles([c for _, c in enemy_l], roles, {c: r for r, c in enemy_l if r})
    me_role = ally.get(me_cid) if me_cid in ally else main_role
    p = plan({r: c for c, r in ally.items()}, {r: c for c, r in enemy.items()}, team, me_role, champs)
    p["source"] = source
    # roles informadas pela fonte (ou Smite) não são "incertas"
    for r, c in enemy_l:
        if r:
            p["enemyRoleConfidence"][r] = 1.0
    if a.html:
        print(f"[ok] página: {write_page(p, Path(a.html))}", file=sys.stderr)
    if a.json:
        print(json.dumps(p, ensure_ascii=False))
    else:
        print_plan(p)


if __name__ == "__main__":
    main()
