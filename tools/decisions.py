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
"""
from __future__ import annotations

import math
from collections import defaultdict

from zones import zone_abs, zone_rel

EPIC = ("DRAGON", "HORDE", "RIFTHERALD", "BARON_NASHOR", "ATAKHAN")
EPIC_PT = {"DRAGON": "dragão", "HORDE": "vastilarvas", "RIFTHERALD": "arauto", "BARON_NASHOR": "barão", "ATAKHAN": "atakhan"}
PITS = {"top": (5007, 10471), "bot": (9866, 4414)}
PIT_OF = {"DRAGON": "bot", "HORDE": "top", "RIFTHERALD": "top", "BARON_NASHOR": "top", "ATAKHAN": "top"}
LANE_ROLES = {"top": ("TOP",), "mid": ("MIDDLE",), "bot": ("BOTTOM", "UTILITY")}
EPIC_WEIGHT = 400   # peso de um objetivo épico no placar da janela (aprox. em ouro)
GOOD, BAD = 400, -400


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
        out["dragão"] = (dragons[-1] + spawn_floor(timers["DRAGON"]["respawn"])) if dragons else spawn_floor(timers["DRAGON"]["first"])
    for k in ("HORDE", "RIFTHERALD"):
        if timers.get(k) and not any(o["type"] == k for o in objs):
            out[EPIC_PT[k]] = spawn_floor(timers[k]["first"])
    if timers.get("BARON_NASHOR"):
        b = sorted(o["ts"] for o in objs if o["type"] == "BARON_NASHOR")
        out["barão"] = (b[-1] + spawn_floor(timers["BARON_NASHOR"]["respawn"])) if b else spawn_floor(timers["BARON_NASHOR"]["first"])
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
        signals = []
        if state.get("hp") is not None and state["hp"] < 50 and mo["kind"] in ("lane", "travessia", "objetivo"):
            signals.append(f"vida {state['hp']}%")
        if state.get("bolso") is not None and state["bolso"] >= 1300 and T < 30 * 60000 and mo["kind"] in ("lane", "travessia", "objetivo", "morte"):
            signals.append(f"{state['bolso']:,} de ouro no bolso".replace(",", "."))
        lane_state = None
        if mo["kind"] == "lane" and mo["target"]:
            roles = LANE_ROLES[mo["target"]]
            al = [p for p in s["participants"] if p["team"] == team and p.get("role") in roles]
            en = [p for p in s["participants"] if p["team"] != team and p.get("role") in roles]
            f_ = lambda ps, k: [before["players"][str(p["pid"])].get(k) for p in ps]
            lv_a, lv_e = sum(f_(al, "level")), sum(f_(en, "level"))
            hp_a = [h for h in f_(al, "hpPct") if h is not None]
            hp_e = [h for h in f_(en, "hpPct") if h is not None]
            lane_state = {"nivel": f"{lv_a}×{lv_e}",
                          "vida": (f"{round(sum(hp_a) / len(hp_a))}%×{round(sum(hp_e) / len(hp_e))}%" if hp_a and hp_e else None)}
            if lv_e - lv_a >= 2:
                signals.append(f"lane inimiga com {lv_e - lv_a} níveis a mais")
            if hp_a and hp_e and sum(hp_e) / len(hp_e) - sum(hp_a) / len(hp_a) >= 30:
                signals.append("lane inimiga com bem mais vida")
        ej = None
        if enemy_jg:
            e = before["players"][str(enemy_jg["pid"])]
            tgt = pm[mo["minute"]]["players"][str(fid)] if mo["minute"] else me
            if e["x"] is not None and tgt["x"] is not None:
                d = int(math.hypot(e["x"] - tgt["x"], e["y"] - tgt["y"]))
                ej = {"zona": zone_rel(e["x"], e["y"], team), "dist": d}
                if mo["kind"] in ("lane", "travessia") and d <= 3000:
                    signals.append(f"jungle inimigo a ~{d:,} (frame {before['time']})".replace(",", "."))
        alive = alive_objectives(s, T, timers)
        soon = spawning_soon(s, T, timers)
        if soon and mo["kind"] in ("recall", "lane", "travessia"):
            signals.append(f"{'/'.join(soon)} nascendo em até 1 min")
        side_of = {"dragão": "bot", "ancião": "bot", "barão": "top", "arauto": "top", "vastilarvas": "top"}
        if mo["kind"] in ("lane", "travessia") and mo.get("toSide"):
            far = [o for o in alive if side_of.get(o) and side_of[o] != mo["toSide"]]
            if far:
                signals.append(f"{'/'.join(far)} vivo do outro lado (estimado)")
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
        score = dgold + EPIC_WEIGHT * (len(ep_a) - len(ep_e))
        verdict = "boa" if score >= GOOD else "ruim" if score <= BAD else "neutra"
        involved = sum(1 for k in kills if fid in {k["killer"], *k["assists"]} and P.get(k["victim"], {}).get("team") != team)
        out.append({
            "ts": T, "time": f"{T // 60000:02d}:{T // 1000 % 60:02d}", "kind": mo["kind"], "what": mo["what"],
            "state": state, "lane": lane_state, "enemyJungle": ej, "alive": alive, "signals": signals,
            "window": f"{before['time'] if mo['minute'] else f'{max(0, T - 30000) // 60000:02d}:{max(0, T - 30000) // 1000 % 60:02d}'}"
                      f"–{t1 // 60000:02d}:{t1 // 1000 % 60:02d}",
            "result": {"abatesNossos": len(ours), "abatesDeles": len(theirs), "comVoce": involved,
                       "objetivosNossos": [o["name"] for o in ep_a], "objetivosDeles": [o["name"] for o in ep_e],
                       "estruturas": [o["name"] for o in objs if o["type"] in ("TOWER", "INHIBITOR")],
                       "ouroTime": dgold},
            "score": score, "verdict": verdict,
        })
    return out
