"""Revisão de decisões do jogador em foco (pensado para jungle).

Uma "decisão" é um momento em que o jogador se compromete com algo que aparece nos dados:
  - lane    : entrou numa lane entre um frame e o seguinte (gank ou visita);
  - travessia: trocou de metade do mapa (lado do barão <-> lado do dragão);
  - objetivo: participou de um objetivo épico;
  - ausente : o inimigo pegou um objetivo épico com você vivo e longe (> 4.000);
  - recall  : voltou à base (compras na Match-V5; inferido na fonte LCU);
  - morte   : morreu.
Para cada uma: o ESTADO na hora (frame anterior: vida, mana, ouro no bolso, nível; a lane de destino; onde
estava o jungle inimigo; objetivos vivos) e o RESULTADO na janela seguinte (abates, objetivos, variação do ouro
do time). O veredito é pelo resultado; os "sinais" são o que dava para ver na hora. Posições só a cada minuto.
Pesos, cortes e quais sinais valem foram calibrados com o histórico do usuário (tools/calibrate.py); os sinais que
não anteciparam resultado pior ficam em `weakSignals`, fora de `signals`.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict

from common import DATA
from zones import zone_abs, zone_rel

EPIC = ("DRAGON", "HORDE", "RIFTHERALD", "BARON_NASHOR", "ATAKHAN")
EPIC_PT = {"DRAGON": "dragão", "HORDE": "vastilarvas", "RIFTHERALD": "arauto", "BARON_NASHOR": "barão", "ATAKHAN": "atakhan"}
PITS = {"top": (5007, 10471), "bot": (9866, 4414)}
PIT_OF = {"DRAGON": "bot", "HORDE": "top", "RIFTHERALD": "top", "BARON_NASHOR": "top", "ATAKHAN": "top"}
LANE_ROLES = {"top": ("TOP",), "mid": ("MIDDLE",), "bot": ("BOTTOM", "UTILITY")}

# ----------------------------------------------------------------------------- calibração
# Calibrado em 2026-11-11 por `python tools/calibrate.py all --write` com 97 partidas de Summoner's Rift (> 10 min)
# do usuário (90 de jungle) e 2.918 decisões. Se data/calibration.json existir (gerado pelo mesmo comando), ele
# substitui estes valores (ver _load_calibration); sem o arquivo (ex.: clone novo, data/ fica fora do git) valem estes.
# Antes (a olho): EPIC_WEIGHT 400 para qualquer épico (cada vastilarva contava 400), GOOD/BAD ±400.
#
# Peso dos épicos = quanto um épico a mais vale em chance de vitória, convertido em ouro: regressão logística
# vitória ~ diferença de ouro + diferença de épicos por tipo, nos minutos 10/14/18/22/26/30 (514 amostras), com
# bootstrap por partida para o IC90. É o valor ALÉM do ouro (o ouro do épico já está no Δouro da janela).
#   - dragão 750 (403 eventos; estimativa 760, IC90 ~0..1.900): o único com amostra e efeito próprios;
#   - atakhan 750 (42 eventos; estimativa 2.277, IC90 741..5.166): limite inferior do IC90, por cautela;
#   - vastilarva 100 POR LARVA (291 eventos; própria ~0): um acampamento de 3 = um épico padrão;
#   - padrão 300 (épico qualquer, larva = 1/3; IC90 -133..1.013): arauto, barão e ancião — arauto deu negativo
#     (-1.032, IC enorme), barão instável (o bônus de 1.500 de ouro já entra no Δouro) e o ancião só aparece em
#     12 partidas, quase sempre depois dos 30 min. Não há dado para medir o valor real do ancião.
EPIC_WEIGHT = 300   # peso de um objetivo épico no placar da janela (em ouro); usado para tipos sem peso próprio
EPIC_WEIGHTS: dict[str, int] = {"DRAGON": 750, "ATAKHAN": 750, "HORDE": 100}   # por evento (HORDE = cada larva)
# GOOD/BAD = terços da distribuição do placar das 2.918 janelas (percentis 67 e 33: 536 e -711, arredondados a 50):
# ~1/3 das decisões "boa", ~1/3 "ruim". A distribuição é assimétrica (mediana -98): as perdas grandes pesam mais.
GOOD, BAD = 550, -700
# Limiares dos sinais (o que conta como "vida baixa", "ouro no bolso" etc.). `calibrate.py thresholds` testou
# alternativas (vida 30-70, bolso 800-2.500, jungle 1.500-5.000, níveis 1-3, vida da lane 10-50): nenhuma fez um
# sinal fraco virar sinal de resultado pior, e "lane níveis" é mais nítido com 2; ficaram os mesmos.
LIMITS = {"vida": 50, "bolso": 1300, "bolsoAte": 30 * 60000, "jungleDist": 3000, "laneNiveis": 2, "laneVida": 30}
# Sinais (categoria -> texto, tipos de decisão em que ele é avaliado). Os de WEAK_SIGNALS não antecederam
# resultado pior no histórico: saem de `signals` e vão para `weakSignals` (a página e o TSV mostram só `signals`).
# Medido com `calibrate.py signals` (placar da janela com × sem o sinal; mesma partida = pareado):
#   - lane níveis (n=50): -711 (IC95 ±518); 42% "ruim" × 32%; vitória 19% × 60%. Na mesma partida só -197
#     (±480): marca mais a partida perdida do que a decisão, mas antecede janela pior. MANTIDO.
#   - objetivo vivo do outro lado (n=263): -179 (±244); 39% "ruim" × 27% (±7); épico perdido na janela em 59% ×
#     21%. MANTIDO.
#   - lane vida (n=52, -44 ±501) e objetivo nascendo (n=412, -80 ±181): só tendência. FRACOS.
#   - vida baixa (n=145, +303), jungle inimigo perto (n=132, +285), ouro no bolso (n=324, +538 ±213, melhor até
#     na mesma partida): não antecedem nada pior; bolso cheio acompanha quem está ganhando. FRACOS.
SIGNALS = {
    "vida baixa": (r"^vida \d+%", ("lane", "travessia", "objetivo")),
    "ouro no bolso": (r"ouro no bolso", ("lane", "travessia", "objetivo", "morte")),
    "lane níveis": (r"níveis? a mais", ("lane",)),
    "lane vida": (r"bem mais vida", ("lane",)),
    "jungle inimigo perto": (r"^jungle inimigo", ("lane", "travessia")),
    "objetivo nascendo": (r"nascendo em até", ("recall", "lane", "travessia")),
    "objetivo vivo do outro lado": (r"vivo do outro lado", ("lane", "travessia")),
}
WEAK_SIGNALS: set[str] = {"vida baixa", "ouro no bolso", "lane vida", "jungle inimigo perto", "objetivo nascendo"}
CALIBRATION_FILE = DATA / "calibration.json"
CALIBRATION: dict = {}


def signal_category(text: str) -> str:
    """Normaliza o texto de um sinal para a categoria ("1.945 de ouro no bolso" -> "ouro no bolso")."""
    for cat, (rx, _) in SIGNALS.items():
        if re.search(rx, text):
            return cat
    return "outro"


def _load_calibration() -> None:
    """Lê data/calibration.json (gerado por tools/calibrate.py) por cima dos padrões acima. Arquivo ausente ou
    inválido: ficam os padrões."""
    global EPIC_WEIGHT, GOOD, BAD, CALIBRATION
    try:
        c = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(c, dict):
        return
    CALIBRATION = c
    EPIC_WEIGHT = int(c.get("epicWeight", EPIC_WEIGHT))
    EPIC_WEIGHTS.update({k: int(v) for k, v in (c.get("epicWeights") or {}).items()})
    GOOD, BAD = int(c.get("good", GOOD)), int(c.get("bad", BAD))
    LIMITS.update({k: v for k, v in (c.get("limits") or {}).items() if k in LIMITS})
    if isinstance(c.get("weakSignals"), list):
        WEAK_SIGNALS.clear()
        WEAK_SIGNALS.update(c["weakSignals"])


_load_calibration()


def epic_key(o: dict) -> str:
    return "ELDER" if (o["type"] == "DRAGON" and "ancião" in o.get("name", "")) else o["type"]


def epic_weight(o: dict) -> int:
    return EPIC_WEIGHTS.get(epic_key(o), EPIC_WEIGHT)


def window_score(dgold: int, ours: list[dict], theirs: list[dict]) -> int:
    """Placar da janela: variação do ouro do time + peso dos objetivos épicos (nossos − deles)."""
    return int(dgold + sum(epic_weight(o) for o in ours) - sum(epic_weight(o) for o in theirs))


def verdict_of(score: float) -> str:
    return "boa" if score >= GOOD else "ruim" if score <= BAD else "neutra"


# ----------------------------------------------------------------------------- horários dos objetivos
def infer_timers(summaries: list[dict]) -> dict:
    """Do próprio histórico: o primeiro abate mais cedo de cada objetivo (=> nasce até esse horário) e o menor
    intervalo entre abates seguidos do mesmo objetivo na mesma partida (=> tempo de renascer, no máximo)."""
    first, gaps, n = {}, defaultdict(list), defaultdict(int)
    for s in summaries:
        if s["mapId"] != 11:
            continue
        by = defaultdict(list)
        for o in s["events"]["objectives"]:
            if o["type"] in EPIC:
                key = "ELDER" if (o["type"] == "DRAGON" and "ancião" in o["name"]) else o["type"]
                by[key].append(o["ts"])
        for k, ts in by.items():
            ts.sort()
            n[k] += 1
            first[k] = min(first.get(k, 10 ** 9), ts[0])
            # vastilarvas morrem em grupo: só conta intervalos > 2 min como "renascer"
            gaps[k] += [b - a for a, b in zip(ts, ts[1:]) if b - a > 120000]
        # ancião: nasce um tempo depois da alma (4º dragão de um time)
        by_team = defaultdict(list)
        for o in s["events"]["objectives"]:
            if o["type"] == "DRAGON" and "ancião" not in o["name"] and o["killerTeam"] in (100, 200):
                by_team[o["killerTeam"]].append(o["ts"])
        souls = [sorted(v)[3] for v in by_team.values() if len(v) >= 4]
        if souls and by.get("ELDER"):
            gaps["ELDER_AFTER_SOUL"].append(min(by["ELDER"]) - min(souls))
            first["ELDER_AFTER_SOUL"] = min(first.get("ELDER_AFTER_SOUL", 10 ** 9), min(by["ELDER"]) - min(souls))
            n["ELDER_AFTER_SOUL"] += 1
    out = {k: {"first": first[k], "respawn": min(gaps[k]) if gaps[k] else None, "games": n[k]} for k in first}
    if "ELDER_AFTER_SOUL" in out:
        out["ELDER_AFTER_SOUL"]["respawn"] = None
    return out


def spawn_floor(ms: int) -> int:
    return (ms // 60000) * 60000


def next_spawns(s: dict, t: int, timers: dict) -> dict[str, int]:
    """Para cada objetivo, o horário (ms) em que ele está/estará disponível, olhando só os abates até t.
    Estimado pelos horários inferidos do histórico. Objetivos que não voltam mais ficam de fora."""
    objs = [o for o in s["events"]["objectives"] if o["ts"] <= t]
    is_elder = lambda o: o["type"] == "DRAGON" and "ancião" in o["name"]
    dragons = sorted(o["ts"] for o in objs if o["type"] == "DRAGON" and not is_elder(o))
    by_team = defaultdict(list)
    for o in objs:
        if o["type"] == "DRAGON" and not is_elder(o) and o["killerTeam"] in (100, 200):
            by_team[o["killerTeam"]].append(o["ts"])
    souls = [sorted(v)[3] for v in by_team.values() if len(v) >= 4]
    out = {}
    if souls and timers.get("ELDER_AFTER_SOUL"):
        elders = sorted(o["ts"] for o in objs if is_elder(o))
        resp = (timers.get("ELDER") or {}).get("respawn")
        out["ancião"] = (elders[-1] + spawn_floor(resp)) if (elders and resp) else min(souls) + spawn_floor(timers["ELDER_AFTER_SOUL"]["first"])
    elif timers.get("DRAGON"):
        out["dragão"] = (dragons[-1] + spawn_floor(timers["DRAGON"]["respawn"] or 300000)) if dragons else spawn_floor(timers["DRAGON"]["first"])
    for k in ("HORDE", "RIFTHERALD"):
        if timers.get(k) and not any(o["type"] == k for o in objs):
            out[EPIC_PT[k]] = spawn_floor(timers[k]["first"])
    if timers.get("BARON_NASHOR"):
        b = sorted(o["ts"] for o in objs if o["type"] == "BARON_NASHOR")
        out["barão"] = (b[-1] + spawn_floor(timers["BARON_NASHOR"]["respawn"] or 360000)) if b else spawn_floor(timers["BARON_NASHOR"]["first"])
    return out


def alive_objectives(s: dict, t: int, timers: dict) -> list[str]:
    return [k for k, at in next_spawns(s, t, timers).items() if at <= t]


def spawning_soon(s: dict, t: int, timers: dict, within: int = 60000) -> list[str]:
    return [k for k, at in next_spawns(s, t, timers).items() if t < at <= t + within]


# ----------------------------------------------------------------------------- utilidades
def side(x, y) -> str:
    z = zone_abs(x, y)
    if z.startswith("base"):
        return "base"
    return "top" if y - x > 1200 else "bot" if y - x < -1200 else "centro"


def frame(s: dict, minute: int) -> dict:
    pm = s["perMinute"]
    return pm[max(0, min(minute, len(pm) - 1))]


def frame_before(s: dict, ts: int) -> dict:
    cand = [r for r in s["perMinute"] if r["ts"] <= ts]
    return cand[-1] if cand else s["perMinute"][0]


def team_gold(s: dict, row: dict, team: int) -> int:
    return row["teamDiff100"]["gold"] * (-1 if team == 200 else 1)


# ----------------------------------------------------------------------------- decisões
def review(s: dict, focus: dict, timers: dict, name=lambda pid: str(pid)) -> list[dict]:
    P = {p["pid"]: p for p in s["participants"]}
    team, fid = focus["team"], focus["pid"]
    enemy_jg = next((p for p in s["participants"] if p["team"] != team and p.get("role") == "JUNGLE"), None)
    pm = s["perMinute"]
    moments = []
    try:  # chance de vitória do modelo (winprob.py); sem modelo treinado, fica de fora
        from winprob import prob_fn
        chance = prob_fn(s, team)
    except ImportError:
        chance = None

    def add(ts, kind, what, minute=None, target=None, to_side=None):
        moments.append({"ts": ts, "kind": kind, "what": what, "minute": minute, "target": target, "toSide": to_side})

    # movimentos entre frames
    for m in range(2, len(pm) - 1):
        a, b = pm[m - 1]["players"][str(fid)], pm[m]["players"][str(fid)]
        if a["x"] is None or b["x"] is None:
            continue
        za, zb = zone_rel(a["x"], a["y"], team), zone_rel(b["x"], b["y"], team)
        sa, sb = side(a["x"], a["y"]), side(b["x"], b["y"])
        if zb in ("top", "mid", "bot") and za != zb and not za.startswith("base"):
            add(pm[m - 1]["ts"], "lane", f"foi para a {zb} ({pm[m - 1]['time']}→{pm[m]['time']})", m, zb,
                {"top": "top", "bot": "bot"}.get(zb))
        elif {sa, sb} == {"top", "bot"}:
            add(pm[m - 1]["ts"], "travessia", f"atravessou do lado {'do barão' if sa == 'top' else 'do dragão'} para o "
                f"{'do dragão' if sb == 'bot' else 'do barão'} ({pm[m - 1]['time']}→{pm[m]['time']})", m, None, sb)
    # objetivos
    for o in s["events"]["objectives"]:
        if o["type"] not in EPIC or o["killerTeam"] not in (100, 200):
            continue
        if fid in o["participantsCertain"] and o["killerTeam"] == team:
            add(o["ts"], "objetivo", f"pegou {o['name']}")
        elif fid in o["participantsCertain"]:
            add(o["ts"], "objetivo", f"disputou {o['name']}, mas o inimigo levou (último golpe de {name(o['killer'])})")
        elif o["killerTeam"] != team:
            fr = frame_before(s, o["ts"])
            me = fr["players"][str(fid)]
            pit = (o["x"], o["y"]) if o.get("x") is not None else PITS[PIT_OF[o["type"]]]
            dead = any(k["victim"] == fid and 0 <= o["ts"] - k["ts"] <= 60000 for k in s["events"]["kills"])
            if me["x"] is not None and not dead and math.hypot(me["x"] - pit[0], me["y"] - pit[1]) > 4000:
                add(o["ts"], "ausente", f"inimigo pegou {o['name']} com você longe "
                    f"(~{int(math.hypot(me['x'] - pit[0], me['y'] - pit[1])):,} no frame {fr['time']})".replace(",", "."))
    # recalls e mortes
    for r in s["events"]["recalls"]:
        if r["pid"] == fid and r["kind"].startswith("recall"):
            add(r["ts"], "recall", f"voltou à base ({r['time']})")
    for k in s["events"]["kills"]:
        if k["victim"] == fid:
            add(k["ts"], "morte", f"morreu para {name(k['killer'])} ({k['zoneForVictim'] or '?'})")

    out = []
    for mo in sorted(moments, key=lambda x: x["ts"]):
        T = mo["ts"]
        before = frame_before(s, T if mo["minute"] is None else pm[mo["minute"] - 1]["ts"])
        me = before["players"][str(fid)]
        recalled_since = any(r["pid"] == fid and before["ts"] < r["ts"] < T for r in s["events"]["recalls"])
        died_recently = any(k["victim"] == fid and 0 <= before["ts"] - k["ts"] <= 75000 for k in s["events"]["kills"])
        if mo["kind"] in ("lane", "travessia") and (me.get("hpPct") == 0 or died_recently):
            continue  # estava morto/renascendo: não é decisão de rota
        state = {"hp": me.get("hpPct"), "mana": me.get("manaPct"), "bolso": me["currentGold"], "nivel": me["level"]}
        if recalled_since and mo["kind"] != "recall":
            # o frame é de antes de um recall: vida e ouro do frame não valem mais
            state.update({"hp": None, "mana": None, "bolso": None, "recallAntes": True})
        signals = []   # (categoria, texto)
        on = lambda cat: mo["kind"] in SIGNALS[cat][1]
        if state.get("hp") is not None and state["hp"] < LIMITS["vida"] and on("vida baixa"):
            signals.append(("vida baixa", f"vida {state['hp']}%"))
        if state.get("bolso") is not None and state["bolso"] >= LIMITS["bolso"] and T < LIMITS["bolsoAte"] and on("ouro no bolso"):
            signals.append(("ouro no bolso", f"{state['bolso']:,} de ouro no bolso".replace(",", ".")))
        lane_state = None
        if mo["kind"] == "lane" and mo["target"]:
            roles = LANE_ROLES[mo["target"]]
            al = [p for p in s["participants"] if p["team"] == team and p.get("role") in roles]
            en = [p for p in s["participants"] if p["team"] != team and p.get("role") in roles]
            f_ = lambda ps, k: [before["players"][str(p["pid"])].get(k) for p in ps]
            lv_a, lv_e = sum(f_(al, "level")), sum(f_(en, "level"))
            hp_a = [h for h in f_(al, "hpPct") if h is not None]
            hp_e = [h for h in f_(en, "hpPct") if h is not None]
            d_hp = round(sum(hp_e) / len(hp_e) - sum(hp_a) / len(hp_a)) if hp_a and hp_e else None
            lane_state = {"nivel": f"{lv_a}×{lv_e}",
                          "vida": (f"{round(sum(hp_a) / len(hp_a))}%×{round(sum(hp_e) / len(hp_e))}%" if hp_a and hp_e else None),
                          "dNivel": lv_e - lv_a, "dVida": d_hp}
            if lv_e - lv_a >= LIMITS["laneNiveis"]:
                signals.append(("lane níveis", f"lane inimiga com {lv_e - lv_a} níveis a mais"))
            if d_hp is not None and d_hp >= LIMITS["laneVida"]:
                signals.append(("lane vida", "lane inimiga com bem mais vida"))
        ej = None
        if enemy_jg:
            e = before["players"][str(enemy_jg["pid"])]
            tgt = pm[mo["minute"]]["players"][str(fid)] if mo["minute"] else me
            if e["x"] is not None and tgt["x"] is not None:
                d = int(math.hypot(e["x"] - tgt["x"], e["y"] - tgt["y"]))
                ej = {"zona": zone_rel(e["x"], e["y"], team), "dist": d}
                if on("jungle inimigo perto") and d <= LIMITS["jungleDist"]:
                    signals.append(("jungle inimigo perto", f"jungle inimigo a ~{d:,} (frame {before['time']})".replace(",", ".")))
        alive = alive_objectives(s, T, timers)
        soon = spawning_soon(s, T, timers)
        if soon and on("objetivo nascendo"):
            signals.append(("objetivo nascendo", f"{'/'.join(soon)} nascendo em até 1 min"))
        side_of = {"dragão": "bot", "ancião": "bot", "barão": "top", "arauto": "top", "vastilarvas": "top"}
        if on("objetivo vivo do outro lado") and mo.get("toSide"):
            far = [o for o in alive if side_of.get(o) and side_of[o] != mo["toSide"]]
            if far:
                signals.append(("objetivo vivo do outro lado", f"{'/'.join(far)} vivo do outro lado (estimado)"))
        # resultado na janela
        t0 = before["ts"] if mo["minute"] else T - 30000
        t1 = (pm[mo["minute"]]["ts"] if mo["minute"] else T) + 90000
        kills = [k for k in s["events"]["kills"] if t0 <= k["ts"] <= t1]
        ours = [k for k in kills if P.get(k["victim"], {}).get("team") not in (team, None)]
        theirs = [k for k in kills if P.get(k["victim"], {}).get("team") == team]
        objs = [o for o in s["events"]["objectives"] if t0 <= o["ts"] <= t1 and o["killerTeam"] in (100, 200)
                and o["type"] in EPIC + ("TOWER", "INHIBITOR")]
        ep_a = [o for o in objs if o["killerTeam"] == team and o["type"] in EPIC]
        ep_e = [o for o in objs if o["killerTeam"] != team and o["type"] in EPIC]
        after = frame_before(s, t1)
        dgold = team_gold(s, after, team) - team_gold(s, frame_before(s, t0), team)
        score = window_score(dgold, ep_a, ep_e)
        verdict = verdict_of(score)
        involved = sum(1 for k in kills if fid in {k["killer"], *k["assists"]} and P.get(k["victim"], {}).get("team") != team)
        out.append({
            "ts": T, "time": f"{T // 60000:02d}:{T // 1000 % 60:02d}", "kind": mo["kind"], "what": mo["what"],
            "state": state, "lane": lane_state, "enemyJungle": ej, "alive": alive, "toSide": mo.get("toSide"),
            "signals": [x for c, x in signals if c not in WEAK_SIGNALS],
            "weakSignals": [x for c, x in signals if c in WEAK_SIGNALS],
            "signalCats": [c for c, _ in signals],
            "window": f"{before['time'] if mo['minute'] else f'{max(0, T - 30000) // 60000:02d}:{max(0, T - 30000) // 1000 % 60:02d}'}"
                      f"–{t1 // 60000:02d}:{t1 // 1000 % 60:02d}",
            "result": {"abatesNossos": len(ours), "abatesDeles": len(theirs), "comVoce": involved,
                       "objetivosNossos": [o["name"] for o in ep_a], "objetivosDeles": [o["name"] for o in ep_e],
                       "epicosNossos": [epic_key(o) for o in ep_a], "epicosDeles": [epic_key(o) for o in ep_e],
                       "estruturas": [o["name"] for o in objs if o["type"] in ("TOWER", "INHIBITOR")],
                       "ouroTime": dgold,
                       "chance": [round(100 * chance(t0)), round(100 * chance(t1))] if chance else None},
            "score": score, "verdict": verdict,
        })
    return out
