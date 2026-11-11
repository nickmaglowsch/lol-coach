"""Renderiza um relatório a partir de um SPEC de análise (JSON) + o resumo da partida.

  lol render reports/<data>-<gameId>-<tema>.json        -> escreve .md e .html ao lado e imprime os caminhos
  lol render <spec.json> --only html|md
  lol render --example                                   -> imprime um spec de exemplo comentado

O spec guarda só o que é JULGAMENTO (vereditos, resumo, recomendações, quais minutos/lutas destacar).
Tudo que é DADO (séries de ouro, posições, kills, objetivos, zonas) é lido de data/summaries/<gameId>.json,
então o relatório nunca diverge dos dados. O .html é a página para publicar como Claude Artifact.

Markup aceito nos textos do spec: **negrito**; horários como 15:31 viram <time> automaticamente.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

from common import ROOT, mmss, parse_time, resolve_player
from landmarks import LANDMARKS
from positions import from_summary
import report_parts as rp
from report_parts import TIME_RE, VERDICTS, mk
from summarize import ROLE_PT, get_summary
from zones import zone_abs, zone_rel

TEMPLATE = Path(__file__).parent / "templates" / "report.html"
# Minimapa oficial do Summoner's Rift (arte da Riot Games, via League of Legends Wiki; 512×512).
# É embutido como data: URI porque a página do artifact não pode carregar imagens de outros sites.
MINIMAP = Path(__file__).parent / "templates" / "assets" / "sr_minimap_512.png"
EPIC = {"DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN"}
ZONE_SHORT_PT = {"top": "top", "mid": "mid", "bot": "bot", "river": "rio", "jungle_ally": "jungle",
                 "jungle_enemy": "jungle inimiga", "base_ally": "base", "base_enemy": "base inimiga"}
LIMITS_SHORT = [
    "A posição de cada jogador só é registrada uma vez por minuto; o que acontece entre esses registros é estimado ou desconhecido.",
    "Ganks que não terminam em kill não aparecem; não dá para saber se houve tentativa sem sucesso.",
    "Flash, feitiços, HP, smite, pings, chat e voz não estão nos dados, e ninguém teve intenção atribuída.",
]

EXAMPLE = {
    "gameId": 1234567890, "focus": "me", "alias": "Eu", "opponent": None, "tema": "disputa-jungle",
    "title": "Cho'Gath × Shaco: o que os dados dizem",
    "note": "opcional: aviso no topo (ex.: qual partida foi analisada e por quê)",
    "tldr": [{"title": "Título curto do card", "text": "até 5 cards; use **negrito** e horários 15:31"}, "ou só o texto"],
    "timeline": [["06:00", "Eu mato a Tristana", "~390 da torre externa vermelha"]],
    "gold": {"bands": [["14:00", "18:36", "luta bot + 4 objetivos"]],
             "notes": [["20:00", "team", "20:00 −6.475 (pior momento)"], ["40:00", "duel", "+4.141 sobre o jungle inimigo"]]},
    "verdicts": [{"q": "O Yorick estava controlado até os 10 min?", "verdict": "REFUTADO", "label": "opcional",
                  "points": ["cada ponto com minuto e dado"]}],
    "strip": {"from": 1, "to": 15, "eyebrow": "Pergunta 2 em detalhe", "caption": "opcional",
              "marks": {"6": [["a", "gank bot"], ["e", "Shaco top 6:43"]]}},
    "pathMap": {"from": 1, "to": 15, "deathsOf": ["Ekko"], "caption": "opcional"},
    "laneChart": {"role": "TOP", "from": 2, "to": 13, "marks": ["6:43", "12:32"], "caption": "opcional"},
    "fights": [{"title": "A luta do bot", "eyebrow": "Pergunta 4", "from": "15:31", "to": "15:53",
                "positionsAt": "15:00", "towers": ["torre externa bot vermelha"], "arrows": [["Akali", "15:53"]],
                "extraSteps": [["16:19–18:33", "e", "caem 3 torres, arauto e dragão"]], "caption": "opcional"}],
    "objectives": {"labels": {"36:58": "Dragão (roubo?)"}, "caption": "opcional"},
    "replay": {"start": "15:00", "jumps": [["15:31", "Luta do bot"]]},
    "sections": [{"title": "Foco extra", "eyebrow": "opcional", "points": ["..."]}],
    "recs": [{"title": "máx. 3", "text": "acionável e mensurável"}],
    "limitations": ["extras específicos desta análise"],
    "artifactUrl": "preenchido depois de publicar",
}


# ----------------------------------------------------------------------------- texto
def warn(msg: str) -> None:
    print(f"[aviso] {msg}", file=sys.stderr)


def todos(obj, path="") -> list[str]:
    """Caminhos dos textos do spec que ainda têm TODO (esqueleto do `lol new` não preenchido)."""
    if isinstance(obj, str):
        return [path or "(raiz)"] if "TODO" in obj else []
    if isinstance(obj, list):
        return [t for i, v in enumerate(obj) for t in todos(v, f"{path}[{i}]")]
    if isinstance(obj, dict):
        return [t for k, v in obj.items() for t in todos(v, f"{path}.{k}" if path else k)]
    return []


def lint(spec: dict, draft: bool = False) -> None:
    left = todos(spec)
    if left and not draft:
        raise SystemExit(f"Spec ainda tem {len(left)} TODO (ex.: {', '.join(left[:4])}). Preencha antes de publicar, "
                         "ou use `lol render --draft` para pré-visualizar.")
    if left:
        warn(f"rascunho: {len(left)} TODO ainda no spec")
    if len(spec.get("recs", [])) > 3:
        raise SystemExit("Spec inválido: no máximo 3 recomendações (CLAUDE.md §4).")
    if len(spec.get("tldr", [])) > 5:
        warn("resumo com mais de 5 linhas")
    for v in spec.get("verdicts", []):
        if v.get("verdict", "").upper() not in VERDICTS:
            raise SystemExit(f'Veredito inválido em "{v.get("q")}": use {", ".join(VERDICTS)}')
        for p in v.get("points", []):
            # partida única: cada ponto precisa de minuto; várias partidas: de minuto ou de contagem/número
            ok = re.search(r"\d", p) if spec.get("mode") == "multi" else TIME_RE.search(p)
            if not ok:
                warn(f'ponto sem minuto/dado (regra de honestidade): "{p[:70]}"')


# ----------------------------------------------------------------------------- dados
class Ctx:
    def __init__(self, spec: dict, s: dict):
        self.spec, self.s = spec, s
        self.P = {p["pid"]: p for p in s["participants"]}
        self.focus = resolve_player(s["participants"], spec.get("focus", "me"))
        if not self.focus:
            raise SystemExit(f'Jogador em foco "{spec.get("focus", "me")}" não encontrado na partida.')
        self.alias = spec.get("alias") or self.focus["riotId"].split("#")[0]
        # aliados aparecem pelo nick (gameName do Riot ID, sem a tag); "aliases" no spec troca por apelido
        # (chave: Riot ID, nick ou campeão). "allyNames": "champion" volta a usar o campeão.
        self.ally_mode = spec.get("allyNames", "nick")
        self.aliases = {k.lower(): v for k, v in spec.get("aliases", {}).items()}
        opp = spec.get("opponent")
        self.opp = resolve_player(s["participants"], opp) if opp else self.P.get(self.focus.get("laneOpponent"))
        self.team = self.focus["team"]
        self.pos = from_summary(s)
        self.pm = s["perMinute"]

    def side(self, team) -> str:
        return "a" if team == self.team else ("e" if team in (100, 200) else "n")

    def name(self, pid) -> str:
        p = self.P.get(pid)
        if not p:
            return "minion/torre"
        if p is self.focus:
            return self.alias
        for key in (p["riotId"], p["riotId"].split("#")[0], p["champion"]):
            if key.lower() in self.aliases:
                return self.aliases[key.lower()]
        if p["team"] == self.team and self.ally_mode == "nick" and not p["riotId"].startswith("?"):
            return p["riotId"].split("#")[0]
        return p["champion"]

    def who(self, pid) -> str:
        """Nome + campeão quando o nome não é o campeão: 'fulano (Vladimir)'."""
        p = self.P.get(pid)
        n = self.name(pid)
        return n if (not p or n == p["champion"]) else f'{n} ({p["champion"]})'

    def player(self, sel) -> dict:
        p = resolve_player(self.s["participants"], sel)
        if not p and str(sel).lower() == self.alias.lower():
            p = self.focus
        if not p:
            raise SystemExit(f'"{sel}" não encontrado na partida.')
        return p

    def row(self, minute: int) -> dict:
        return self.pm[min(max(0, minute), len(self.pm) - 1)]

    def at(self, pid, ts):
        e = self.pos.at(pid, ts)
        return [int(e["x"]), int(e["y"])] if e else None


def gold_data(c: Ctx) -> dict:
    sign = -1 if c.team == 200 else 1
    minutes = [round(r["ts"] / 60000, 2) for r in c.pm]
    team = [sign * r["teamDiff100"]["gold"] for r in c.pm]
    duel = None
    if c.opp:
        duel = [r["players"][str(c.focus["pid"])]["gold"] - r["players"][str(c.opp["pid"])]["gold"] for r in c.pm]
    g = c.spec.get("gold", {})
    notes = []
    for t, series, text in g.get("notes", []):
        i = min(range(len(minutes)), key=lambda k: abs(minutes[k] - parse_time(t) / 60000))
        arr = duel if (series == "duel" and duel) else team
        notes.append([minutes[i], arr[i], text])
    if not g.get("notes"):
        i = min(range(len(team)), key=lambda k: team[k])
        if team[i] < 0:
            notes.append([minutes[i], team[i], f"{c.pm[i]['time']} {team[i]:+,d} (pior momento)".replace(",", ".")])
    bands = [[parse_time(a) / 60000, parse_time(b) / 60000, lbl] for a, b, lbl in g.get("bands", [])]
    return {"minutes": minutes, "team": team, "duel": duel, "notes": notes, "bands": bands}


def kill_words(c: Ctx, k: dict) -> str:
    v, kl = c.name(k["victim"]), c.name(k["killer"])
    ass = [c.name(a) for a in k["assists"]]
    extra = f" (+{', '.join(ass)})" if ass else ""
    if c.side(k["victimTeam"]) == "a":
        return f"**{v} morre** para {kl}{extra}"
    return f"**{kl} mata {v}**{extra}"


def bucket(ts: int) -> int:
    """Minuto do horário exibido (05:59.6 aparece como 06:00 -> minuto 6)."""
    return int(round(ts / 1000)) // 60


def where(k: dict) -> str:
    lm = k.get("landmark") or ""
    if "~" not in lm:
        return ""
    name, dist = lm.rsplit(" ~", 1)
    return f" (a ~{int(dist):,} de: {name})".replace(",", ".")


def strip_data(c: Ctx) -> dict | None:
    st = c.spec.get("strip")
    if not st or c.s["mapId"] != 11:
        return None
    lo, hi = st.get("from", 1), st.get("to", 15)
    fid = c.focus["pid"]
    custom = st.get("marks")
    cells = []
    for m in range(lo, hi + 1):
        v = c.row(m)["players"][str(fid)]
        z = zone_abs(v["x"], v["y"])
        side = "base" if z.startswith("base") else ("top" if v["y"] - v["x"] > 1200 else ("bot" if v["y"] - v["x"] < -1200 else "mid"))
        marks = []
        if custom is not None:
            marks = [list(x) for x in custom.get(str(m), [])]
        else:
            for k in c.s["events"]["kills"]:
                if bucket(k["ts"]) != m:
                    continue
                inv = {k["killer"], *k["assists"]}
                zone = ZONE_SHORT_PT.get(zone_rel(k["x"], k["y"], c.team), "")
                if fid in inv:
                    marks.append(["a", f"kill {zone}"])
                elif k["victim"] == fid:
                    marks.append(["e", f"✝{c.alias}"])
                elif c.opp and c.opp["pid"] in inv and c.side(k["victimTeam"]) == "a":
                    marks.append(["e", f"{c.opp['champion']} {zone} ✝{c.name(k['victim'])}"])
            for o in c.s["events"]["objectives"]:
                if bucket(o["ts"]) == m and o["type"] in EPIC and fid in o["participantsCertain"]:
                    marks.append(["a", o["name"].split(" (")[0]])
            seen, uniq = set(), []
            for mk_ in marks:
                if tuple(mk_) not in seen:
                    seen.add(tuple(mk_)); uniq.append(mk_)
            marks = uniq[:2]
        cells.append({"minute": m, "side": side, "title": f"{m}:00 ({v['x']}, {v['y']})", "marks": marks})
    return {"cells": cells}


def path_data(c: Ctx) -> dict | None:
    pm_ = c.spec.get("pathMap")
    if not pm_ or c.s["mapId"] != 11:
        return None
    lo, hi = pm_.get("from", 1), pm_.get("to", 15)
    xy = lambda pid: [[c.row(m)["players"][str(pid)]["x"], c.row(m)["players"][str(pid)]["y"]] for m in range(lo, hi + 1)]
    t0, t1 = lo * 60000 - 60000, hi * 60000 + 30000
    fid = c.focus["pid"]
    kills = [{"p": [k["x"], k["y"]], "label": k["time"]} for k in c.s["events"]["kills"]
             if k["killer"] == fid and t0 <= k["ts"] <= t1]
    who = {c.player(n)["pid"] for n in pm_.get("deathsOf", [])} | {fid}
    deaths = [{"p": [k["x"], k["y"]], "t": c.side(k["victimTeam"]), "label": f"{c.name(k['victim'])} ✝ {k['time']}"}
              for k in c.s["events"]["kills"] if k["victim"] in who and t0 <= k["ts"] <= t1]
    return {"from": lo, "at": hi * 60000, "focus": xy(fid), "opp": xy(c.opp["pid"]) if c.opp else None,
            "kills": merge_close(kills), "deaths": merge_close(deaths)}


def merge_close(items: list[dict], radius: int = 900) -> list[dict]:
    """Junta marcadores a menos de `radius` unidades num rótulo só (ex.: 'Ekko ✝ 02:26 · 06:43')."""
    out: list[dict] = []
    for it in items:
        hit = next((o for o in out if o.get("t") == it.get("t")
                    and ((o["p"][0] - it["p"][0]) ** 2 + (o["p"][1] - it["p"][1]) ** 2) ** .5 < radius), None)
        if hit is None:
            out.append(dict(it, extra=[]))
            continue
        hit["extra"].append(it["p"])
        a, b = hit["label"], it["label"]
        if " ✝ " in a and a.split(" ✝ ")[0] == b.split(" ✝ ")[0]:
            hit["label"] = a + " · " + b.split(" ✝ ")[1]
        else:
            hit["label"] = a + " · " + b
    return out


def lane_data(c: Ctx) -> dict | None:
    lc = c.spec.get("laneChart")
    if not lc:
        return None
    role = lc["role"].upper()
    me = next((p for p in c.s["participants"] if p["team"] == c.team and p.get("role") == role), None)
    if not me:
        warn(f"role {role} não encontrada"); return None
    en = c.P.get(me.get("laneOpponent"))
    lo, hi = lc.get("from", 2), lc.get("to", 13)
    vals = [c.row(m)["players"][str(me["pid"])].get("goldDiff") or 0 for m in range(lo, hi + 1)]
    marks = [[parse_time(t) / 60000, t] for t in lc.get("marks", [])]
    return {"from": lo, "values": vals, "marks": marks, "ally": c.name(me["pid"]), "enemy": en["champion"] if en else "?"}


def fight_data(c: Ctx, f: dict) -> dict:
    t0, t1 = parse_time(f["from"]), parse_time(f["to"])
    at = parse_time(f.get("positionsAt", f["from"]))
    players, placed = [], []
    for p in c.s["participants"]:
        xy = c.at(p["pid"], at)
        if not xy:
            continue
        px, py = xy[0] / 14900 * 300, 300 - xy[1] / 14900 * 300
        dx, dy = 8, 4
        for _ in range(6):  # evita rótulos sobrepostos (greedy)
            if any(abs(px + dx - qx) < 34 and abs(py + dy - qy) < 10 for qx, qy in placed):
                dy += 11 if py < 280 else -11
            else:
                break
        if px > 250:
            dx = -8 - 6 * len(c.name(p["pid"]))
        placed.append((px + dx, py + dy))
        e = c.pos.at(p["pid"], at)
        players.append({"p": xy, "t": c.side(p["team"]), "name": c.name(p["pid"]), "dx": dx, "dy": dy,
                        "cid": p.get("championId"), "focus": p is c.focus, "dead": bool(e and e["dead"])})
    kills = [k for k in c.s["events"]["kills"] if t0 <= k["ts"] <= t1]
    deaths = [{"p": [k["x"], k["y"]], "t": c.side(k["victimTeam"])} for k in kills]
    towers = []
    for name in f.get("towers", []):
        lm = next((l for l in LANDMARKS if l[0] == name), None)
        if lm:
            towers.append({"p": [lm[1], lm[2]], "t": c.side(lm[3])})
        else:
            warn(f'landmark "{name}" não existe (veja tools/landmarks.py)')
    arrows = []
    for champ, t in f.get("arrows", []):
        p = c.player(champ)
        start = c.at(p["pid"], at)
        hit = [k for k in kills if p["pid"] == k["killer"] and abs(k["ts"] - parse_time(t)) <= 5000]
        end = [hit[0]["x"], hit[0]["y"]] if hit else c.at(p["pid"], parse_time(t))
        if start and end:
            arrows.append({"from": start, "to": end, "t": c.side(p["team"])})
    steps = [[k["time"], c.side(c.P[k["killer"]]["team"]) if k["killer"] in c.P else "n",
              kill_words(c, k) + where(k)] for k in kills]
    for o in c.s["events"]["objectives"]:
        if t0 <= o["ts"] <= t1 and o["type"] != "TURRET_PLATE":
            who = "Seu time" if c.side(o["killerTeam"]) == "a" else "Inimigos"
            parts = ", ".join(c.name(x) for x in o["participantsCertain"]) or "sem campeão creditado"
            steps.append([o["time"], c.side(o["killerTeam"]), f'**{who} pegam {o["name"]}** ({parts})'])
    steps += [list(x) for x in f.get("extraSteps", [])]
    steps.sort(key=lambda st: parse_time(re.split(r"[–/ -]", st[0].strip())[0]))
    return {"at": at, "players": players, "deaths": deaths, "towers": towers, "arrows": arrows, "steps": steps}


def objectives_data(c: Ctx) -> dict:
    labels = {k: v for k, v in c.spec.get("objectives", {}).get("labels", {}).items()}
    items, horde = [], None
    for o in c.s["events"]["objectives"]:
        if o["type"] not in EPIC or o["killerTeam"] not in (100, 200):
            continue
        t = c.side(o["killerTeam"])
        if o["type"] == "HORDE":
            if horde and horde["t"] == t and o["ts"] - horde["_ts"] <= 90000:
                horde["n"] += 1; horde["_ts"] = o["ts"]; horde["label"] = f"Vastilarvas ×{horde['n']}"
                continue
            horde = {"m": o["ts"] / 60000, "t": t, "time": o["time"], "label": "Vastilarvas", "n": 1, "_ts": o["ts"]}
            items.append(horde)
            continue
        label = o["name"][:1].upper() + o["name"][1:]
        items.append({"m": o["ts"] / 60000, "t": t, "time": o["time"], "label": labels.get(o["time"], label)})
    for it in items:
        it.pop("_ts", None); it.pop("n", None)
        it["label"] = labels.get(it["time"], it["label"])
    return {"items": items}


NEXUS = [(1520, 1580, 100), (13280, 13320, 200)]  # centro aproximado dos nexus (entre as torres do nexus)


def structures_data(c: Ctx) -> list[dict] | None:
    """Torres, inibidores e nexus (coordenadas exatas de landmarks.py) com os horários em que caíram."""
    if c.s["mapId"] != 11:
        return None
    out = []
    for name, x, y, team, kind, _ in LANDMARKS:
        if kind in ("torre", "inibidor"):
            out.append({"x": x, "y": y, "t": c.side(team), "k": "tower" if kind == "torre" else "inhib", "down": [], "name": name})
    for x, y, team in NEXUS:
        out.append({"x": x, "y": y, "t": c.side(team), "k": "nexus", "down": [], "name": "nexus"})
    for o in c.s["events"]["objectives"]:
        if o["type"] not in ("TOWER", "INHIBITOR") or o["x"] is None:
            continue
        kind = "tower" if o["type"] == "TOWER" else "inhib"
        cands = [st for st in out if st["k"] == kind]
        st = min(cands, key=lambda st: (st["x"] - o["x"]) ** 2 + (st["y"] - o["y"]) ** 2)
        if ((st["x"] - o["x"]) ** 2 + (st["y"] - o["y"]) ** 2) ** .5 < 400:
            st["down"].append(o["ts"])
    if c.s["winnerTeam"] in (100, 200):
        loser = next(st for st in out if st["k"] == "nexus" and st["t"] == c.side(300 - c.s["winnerTeam"]))
        loser["down"].append(int((c.s["duration"] or 0) * 1000))
    for st in out:
        st.pop("name")
    return out


ICONS = ROOT / "data" / "static" / "champion-icons"


def icons_data(c: Ctx) -> dict:
    """Retratos (data: URI) dos 10 campeões, do cache baixado pelo `lol icons` / `lol fetch`."""
    out, missing = {}, []
    for p in c.s["participants"]:
        cid = p.get("championId")
        f = ICONS / f"{cid}.png"
        if cid is not None and f.exists():
            out[str(cid)] = "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
        else:
            missing.append(p["champion"])
    if missing:
        warn(f"sem retrato de {', '.join(missing)}: rode `lol icons {c.s['gameId']}` com o cliente aberto (usa bolinhas)")
    return out


REPLAY_STEP = 10000  # ms entre amostras de posição (o JS interpola entre elas)


DEC_KIND = {"lane": "rota", "travessia": "travessia", "objetivo": "objetivo", "ausente": "ausente",
            "recall": "recall", "morte": "morte"}
DEC_CLS = {"boa": "ok", "ruim": "bad", "neutra": "unk"}


def decisions_data(c: Ctx) -> list[dict] | None:
    """Revisão de decisões (decisions.py). Padrão: ligada em Summoner's Rift; `"decisions": false` desliga.
    Spec: {"pick": ["14:00", ...]} escolhe os momentos; senão entram os com sinais ou grande impacto (máx. `max`)."""
    cfg = c.spec.get("decisions", {})
    if cfg is False or c.s["mapId"] != 11:
        return None
    from decisions import infer_timers, review
    from summarize import cached_ids
    timers = infer_timers([get_summary(g) for g in cached_ids()])
    allm = review(c.s, c.focus, timers, c.name)
    notes = cfg.get("notes", {})
    if cfg.get("pick"):
        want = {mmss(parse_time(t)) for t in cfg["pick"]}
        chosen = [d for d in allm if d["time"] in want]
    else:
        cand = [d for d in allm if d["signals"] or abs(d["score"]) >= 1000]
        cand.sort(key=lambda d: abs(d["score"]) + 300 * len(d["signals"]), reverse=True)
        chosen, used = [], []
        for d in cand:  # evita dois cards sobre a mesma janela
            if any(abs(d["ts"] - u) < 45000 for u in used):
                continue
            chosen.append(d); used.append(d["ts"])
            if len(chosen) >= cfg.get("max", 10):
                break
    chosen.sort(key=lambda d: d["ts"])
    for d in chosen:
        d["note"] = notes.get(d["time"]) or notes.get(d["time"].lstrip("0"))
    return chosen


def decisions_html(c: Ctx, decs: list[dict]) -> str:
    E = rp.E
    cards = []
    for d in decs:
        st, r = d["state"], d["result"]
        now = [f'vida {st["hp"]}%' if st["hp"] is not None else None, f'{st["bolso"]:,} de ouro no bolso'.replace(",", "."),
               f'nível {st["nivel"]}']
        if d["lane"]:
            now.append(f'lane: níveis {d["lane"]["nivel"]}' + (f', vida {d["lane"]["vida"]}' if d["lane"]["vida"] else "") + " (seu time × inimigo)")
        if d["enemyJungle"]:
            now.append(f'jungle inimigo: {rp.ZONE_LABEL.get(d["enemyJungle"]["zona"], d["enemyJungle"]["zona"])}, a ~{d["enemyJungle"]["dist"]:,}'.replace(",", "."))
        if d["alive"]:
            now.append("vivos (estimado): " + ", ".join(d["alive"]))
        res = [f'abates {r["abatesNossos"]}×{r["abatesDeles"]} (você em {r["comVoce"]})',
               f'ouro do time {r["ouroTime"]:+,d}'.replace(",", ".").replace("-", "−")]
        if r["objetivosNossos"]:
            res.append("seu time pegou " + ", ".join(sorted(set(r["objetivosNossos"]))))
        if r["objetivosDeles"]:
            res.append("inimigo pegou " + ", ".join(sorted(set(r["objetivosDeles"]))))
        chips = "".join(f'<span class="sig">{E(x)}</span>' for x in d["signals"]) or '<span class="sub">nenhum</span>'
        note = f'<p class="dec-note">{rp.mk(d["note"])}</p>' if d.get("note") else ""
        cards.append(f'''<article class="dec"><div class="dec-head"><time>{E(d["time"])}</time><span class="tag">{E(DEC_KIND[d["kind"]])}</span>
<h3>{E(d["what"])}</h3><span class="chip {DEC_CLS[d["verdict"]]}">{E(d["verdict"])}</span></div>
<div class="dec-grid"><div><div class="eyebrow">Na hora</div><p>{E("; ".join(x for x in now if x))}</p></div>
<div><div class="eyebrow">Sinais</div><p>{chips}</p></div>
<div><div class="eyebrow">Resultado ({E(d["window"])})</div><p>{E("; ".join(res))}</p></div></div>{note}</article>''')
    cap = ("O veredito vem do que aconteceu na janela (abates, objetivos e ouro do time), não do que você sabia na hora: "
           "decisão boa às vezes dá errado. Os sinais são o que dava para ver no momento. Posições a cada minuto; "
           "horários dos objetivos estimados pelo seu histórico (`lol timers`).")
    return ('<section id="decisoes"><div class="eyebrow">Momento a momento</div><h2>Revisão de decisões</h2>'
            f'<div class="decs">{"".join(cards)}</div><p class="cap">{rp.mk(cap)}</p></section>')


def replay_data(c: Ctx) -> dict | None:
    rp = c.spec.get("replay", {})
    if rp is False or c.s["mapId"] != 11:
        return None
    dur = int((c.s["duration"] or 0) * 1000) or c.pm[-1]["ts"]
    times = list(range(0, dur + 1, REPLAY_STEP))
    players = []
    order = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", ""]
    for p in sorted(c.s["participants"], key=lambda q: (q["team"] != c.team, order.index(q.get("role", "")) if q.get("role", "") in order else 9)):
        flat = []
        for t in times:
            e = c.pos.at(p["pid"], t)
            flat += [int(e["x"]), int(e["y"]), 1 if e["dead"] else 0] if e else [0, 0, 1]
        players.append({"name": c.name(p["pid"]), "t": c.side(p["team"]), "focus": p is c.focus, "cid": p.get("championId"),
                        "role": ROLE_PT.get(p.get("role", ""), ""), "xy": flat})
    events = []
    for k in c.s["events"]["kills"]:
        if k["x"] is None:
            continue
        killer_side = c.side(c.P[k["killer"]]["team"]) if k["killer"] in c.P else "n"
        txt = re.sub(r"\*\*", "", kill_words(c, k))
        events.append({"ts": k["ts"], "time": k["time"], "k": "kill", "t": killer_side, "v": c.side(k["victimTeam"]),
                       "x": k["x"], "y": k["y"], "text": txt})
    for o in c.s["events"]["objectives"]:
        if o["x"] is None or o["type"] == "TURRET_PLATE":
            continue
        who = "seu time" if c.side(o["killerTeam"]) == "a" else ("inimigo" if o["killerTeam"] else "?")
        events.append({"ts": o["ts"], "time": o["time"], "k": "epic" if o["type"] in EPIC else "build", "ty": o["type"],
                       "t": c.side(o["killerTeam"]), "x": o["x"], "y": o["y"],
                       "text": f'{who} pega {o["name"]}'})
    events.sort(key=lambda e: e["ts"])
    jumps = [[parse_time(t), lbl] for t, lbl in rp.get("jumps", [])]
    jumps += [[d["ts"], f'{d["time"]} {DEC_KIND[d["kind"]]}'] for d in (getattr(c, "decisions", None) or [])]
    jumps += [[parse_time(f["from"]), f.get("title", "Luta " + f["from"])] for f in c.spec.get("fights", [])]
    for it in objectives_data(c)["items"]:
        jumps.append([int(it["m"] * 60000), f'{it["label"]} {it["time"]}'])
    jumps.sort()
    fights = c.spec.get("fights", [])
    if rp.get("start"):
        start = parse_time(rp["start"])
    elif fights:
        start = max(0, parse_time(fights[0]["from"]) - 5000)
    else:
        start = jumps[0][0] if jumps else 600000
    return {"step": REPLAY_STEP, "duration": dur, "blue": c.side(100), "players": players, "events": events,
            "jumps": [{"ts": max(0, t - 5000), "label": l} for t, l in jumps], "start": min(start, dur)}


# ----------------------------------------------------------------------------- HTML
def html_body(c: Ctx, data: dict) -> str:
    sp, s, f = c.spec, c.s, c.focus
    st = f["stats"]
    win = st["win"]
    d = dt.datetime.fromisoformat(s["date"])
    E = lambda x: html.escape(str(x))
    out = []
    # header
    ag = f["aggregates"]
    tiles = [(f'<b class="{"ally" if win else "enemy"}">{"Vitória" if win else "Derrota"}</b>', f'{s["durationStr"]}'),
             (f'<b>{st["kills"]} / {st["deaths"]} / {st["assists"]}</b>', f'{E(c.alias)}, {E(f["champion"])} {E(ROLE_PT.get(f.get("role", ""), ""))}')]
    if c.opp and ag.get("cs10") is not None:
        ocs = c.row(10)["players"][str(c.opp["pid"])]["cs"]
        tiles.append((f'<b>{ag["cs10"]} × {ocs}</b>', f'CS aos 10 min ({E(f["champion"])} × {E(c.opp["champion"])})'))
    team = data["gold"]["team"]
    i = min(range(len(team)), key=lambda k: team[k]) if min(team) < 0 else max(range(len(team)), key=lambda k: team[k])
    cls = "enemy" if team[i] < 0 else "ally"
    tiles.append((f'<b class="{cls}">{team[i]:+,d}</b>'.replace(",", ".").replace("-", "−"),
                  f'{"pior" if team[i] < 0 else "melhor"} diferença de ouro do time ({c.pm[i]["time"]})'))
    allies = ", ".join(c.who(p["pid"]) for p in s["participants"] if p["team"] == c.team)
    enemies = ", ".join(p["champion"] for p in s["participants"] if p["team"] != c.team)
    out.append(f'''<header>
  <div class="eyebrow">Review de {E(s["queue"])} · {d:%d/%m · %H:%M} · jogo {s["gameId"]}</div>
  <h1>{mk(sp.get("title", f"{f['champion']} — partida {s['gameId']}"))}</h1>
  <div class="score">{"".join(f"<div>{b}<span>{t}</span></div>" for b, t in tiles)}</div>
  <p class="sub"><span class="ally">Seu time</span>: {E(allies)}. <span class="enemy">Inimigos</span>: {E(enemies)}. Cada conclusão vem da timeline do jogo (um registro de posição por minuto + kills e objetivos) e recebe um veredito: <strong style="color:var(--ok)">suportado</strong>, <strong style="color:var(--bad)">refutado</strong> ou <strong style="color:var(--unk)">inconclusivo</strong>.</p>
</header>''')
    out += [rp.note(sp), rp.tldr(sp)]
    duel_leg = f'<span><i style="background:var(--gold)"></i>{E(c.alias)} − {E(c.opp["champion"])} (ouro)</span>' if c.opp else ""
    out.append(f'''<section><div class="eyebrow">Ouro minuto a minuto</div><h2>A partida em um gráfico</h2>
  <div class="fig"><div class="legend"><span><i style="background:var(--ally)"></i>Diferença de ouro do time (seu time − inimigo)</span>{duel_leg}</div>
  <div class="scroll"><svg id="goldChart" viewBox="0 0 820 300" role="img" aria-label="Diferença de ouro por minuto"></svg></div>
  {f'<p class="cap">{mk(sp["gold"]["caption"])}</p>' if sp.get("gold", {}).get("caption") else ""}</div></section>''')
    if data.get("replay"):
        out.append(f'''<section id="replay"><div class="eyebrow">Interativo</div><h2>Replay da partida</h2>
<div class="fig rp">
  <div class="rp-top">
    <svg id="replayMap" viewBox="0 0 300 300" role="img" aria-label="Mapa com a posição dos 10 jogadores no instante escolhido"></svg>
    <div class="rp-side">
      <div class="rp-hud"><div><span class="eyebrow">tempo</span><b id="rpClock">00:00</b></div>
        <div><span class="eyebrow">abates</span><b><span class="ally" id="rpKa">0</span> × <span class="enemy" id="rpKe">0</span></b></div>
        <div><span class="eyebrow">ouro do time</span><b id="rpGold">0</b></div></div>
      <div class="rp-hud rp-hud2"><div><span class="eyebrow">dragões</span><b id="rpDrag">0 × 0</b></div>
        <div><span class="eyebrow">barões</span><b id="rpBaron">0 × 0</b></div>
        <div><span class="eyebrow">torres derrubadas</span><b id="rpTower">0 × 0</b></div></div>
      <div class="eyebrow">Últimos eventos</div>
      <ol class="rp-feed" id="rpFeed"></ol>
      <div class="mapkey"><span><i style="background:var(--ally)"></i>seu time</span><span><i style="background:var(--enemy)"></i>inimigos</span><span><i style="background:var(--dead)"></i>estrutura caída</span><span>■ torre · ◆ inibidor/nexus</span><span><i style="background:var(--buff-b);border-radius:50%"></i><i style="background:var(--buff-r);border-radius:50%"></i>buffs</span><span><i style="background:var(--camp);border-radius:50%"></i>campos</span><span>fosso de cima: barão/arauto/larvas · de baixo: dragão</span></div>
      <p class="cap">Posições registradas a cada minuto; entre elas, estimativa pelos locais de kill. ✝ = morto. Torres e inibidores caem no horário real; inibidores voltam após 5 min (regra do jogo).</p>
    </div>
  </div>
  <div class="rp-bar">
    <button type="button" class="btn" id="rpPlay" aria-label="Reproduzir">▶</button>
    <label class="sr" for="rpSpeed">Velocidade</label>
    <select id="rpSpeed" class="btn"><option value="10">10×</option><option value="30" selected>30×</option><option value="60">60×</option></select>
    <label class="chk"><input type="checkbox" id="rpIcons" checked> retratos dos campeões</label>
    <label class="chk" title="As waves são uma simulação pelas regras do jogo, não vêm dos dados"><input type="checkbox" id="rpWaves" checked> waves de minions (simulação)</label>
    <div class="rp-jumps" id="rpJumps"></div>
  </div>
  <svg id="rpTrack" viewBox="0 0 820 84" role="img" aria-label="Linha do tempo: ouro do time e eventos; clique para ir ao momento"></svg>
  <label class="sr" for="rpSlider">Momento da partida</label>
  <input type="range" id="rpSlider" min="0" max="{data["replay"]["duration"]}" step="1000" value="{data["replay"]["start"]}">
</div></section>''')
    out.append(rp.verdicts(sp, "Perguntas e alegações", "Vereditos"))
    if data.get("decisions"):
        out.append(decisions_html(c, data["decisions"]))
    if data.get("strip") or data.get("pathMap") or data.get("lane"):
        stp = sp.get("strip", {})
        parts = [f'<div class="eyebrow">{E(stp.get("eyebrow", "Pathing"))}</div>']
        if data.get("strip"):
            lo, hi = stp.get("from", 1), stp.get("to", 15)
            parts.append(f'''<h2>Onde {E(c.alias)} estava em cada minuto ({lo}–{hi})</h2>
<div class="fig"><div class="legend"><span><i style="background:var(--ally);height:10px"></i>lado bot do mapa</span><span><i style="background:var(--gold);height:10px"></i>lado top</span><span><i style="background:var(--unk);height:10px"></i>centro</span><span><i style="background:var(--line);height:10px"></i>base</span></div>
<div class="strip" id="strip"></div><div class="strip-labels" id="stripLbl"></div><div class="marks" id="marks"></div>
<p class="cap">{mk(stp.get("caption", f"Embaixo de cada minuto: jogadas de {c.alias} (azul) e do adversário (vermelho). Lado bot/top = as duas metades do mapa divididas pela diagonal da mid; um minuto num lado pode ser só farm."))}</p></div>''')
        figs = []
        if data.get("pathMap"):
            pmc = sp["pathMap"]
            opp_leg = (f'<button type="button" class="tog" data-g="opp" aria-pressed="true"><i style="background:var(--enemy);height:8px;width:8px;border-radius:50%"></i>{E(c.opp["champion"])}</button>') if c.opp else ""
            figs.append(f'''<div class="fig"><h3>Caminho dos junglers ({pmc.get("from", 1)}–{pmc.get("to", 15)} min)</h3>
<svg id="mapPath" viewBox="0 0 300 300" role="img" aria-label="Mapa com posições por minuto"></svg>
<div class="legend"><button type="button" class="tog" data-g="focus" aria-pressed="true"><i style="background:var(--ally);height:8px;width:8px;border-radius:50%"></i>{E(f["champion"])} (minuto)</button>{opp_leg}<span style="color:var(--gold)">★ kill de {E(c.alias)}</span><span>✕ morte</span></div>
<p class="cap">{mk(pmc.get("caption", "As linhas tracejadas ligam um minuto ao seguinte; o caminho real entre os pontos não aparece nos dados."))} Clique nos nomes da legenda para mostrar ou esconder cada caminho.</p></div>''')
        if data.get("lane"):
            ln = data["lane"]
            figs.append(f'''<div class="fig"><h3>{E(ln["ally"])} × {E(ln["enemy"])} (ouro)</h3>
<svg id="laneChart" viewBox="0 0 320 230" role="img" aria-label="Diferença de ouro na lane"></svg>
<p class="cap">{mk(sp["laneChart"].get("caption", f"Barras azuis: {ln['ally']} à frente; vermelhas: {ln['enemy']} à frente."))}</p></div>''')
        if figs:
            parts.append('<div class="two">' + "".join(figs) + "</div>")
        out.append("<section>" + "".join(parts) + "</section>")
    for idx, (fs, fd) in enumerate(zip(sp.get("fights", []), data.get("fights", []))):
        steps = "".join(f'<li><time>{E(t)}</time><span class="pip {k}"></span><div>{mk(txt)}</div></li>' for t, k, txt in fd["steps"])
        at = mmss(parse_time(fs.get("positionsAt", fs["from"])))
        out.append(f'''<section><div class="eyebrow">{E(fs.get("eyebrow", "Luta"))}</div><h2>{mk(fs.get("title", f"Luta {fs['from']}–{fs['to']}"))}</h2>
<div class="two"><div class="fig"><h3>Posições às {at}</h3><svg id="mapFight{idx}" viewBox="0 0 300 300" role="img" aria-label="Posições na luta"></svg>
<div class="legend"><span class="ally">● seu time</span><span class="enemy">● inimigos</span><span>✕ mortes na luta</span><span>■ torre</span></div>
{f'<p class="cap">{mk(fs["caption"])}</p>' if fs.get("caption") else ""}</div>
<div class="fig"><h3>Sequência</h3><ol class="steps">{steps}</ol></div></div></section>''')
    if data["objectives"]["items"]:
        cap = sp.get("objectives", {}).get("caption", "Acima da linha, objetivos do seu time; abaixo, do inimigo.")
        out.append(f'''<section><div class="eyebrow">Objetivos</div><h2>Timing de objetivos</h2>
<div class="fig"><div class="scroll"><svg id="objChart" viewBox="0 0 820 190" role="img" aria-label="Linha do tempo dos objetivos épicos"></svg></div><p class="cap">{mk(cap)}</p></div></section>''')
    out += rp.sections(sp)
    out.append(rp.recs(sp, "Para a próxima"))
    lim = LIMITS_SHORT + (["Recalls e roles foram inferidos; dados do histórico do cliente, sem eventos de ward ou de compra."]
                          if s["source"] == "lcu" else []) + sp.get("limitations", [])
    out.append(rp.limits(lim))
    out.append(f'<footer>Gerado pelo lol-coach a partir da timeline da partida {s["gameId"]} '
               f'(fonte: {E(s["source"])}, patch {E(".".join(s["gameVersion"].split(".")[:2]))}). '
               f'Distâncias em unidades do mapa: a lane mid tem ~10.000 de ponta a ponta. '
               f'Minimapa: arte da Riot Games (League of Legends Wiki), usada como conteúdo de fã.</footer>')
    return "\n".join(x for x in out if x)


# ----------------------------------------------------------------------------- Markdown
def md_report(c: Ctx) -> str:
    sp, s, f = c.spec, c.s, c.focus
    st = f["stats"]
    L = [f'# {sp.get("title", "Partida " + str(s["gameId"]))}', "",
         f'{s["queue"]} · {s["date"].replace("T", " ")} · {s["durationStr"]} · **{"vitória" if st["win"] else "derrota"}** · '
         f'{c.alias} = {f["champion"]} {ROLE_PT.get(f.get("role", ""), "")} ({st["kills"]}/{st["deaths"]}/{st["assists"]}) · fonte: {s["source"]}', ""]
    L += rp.md_intro(sp)
    L += ["## Linha do tempo", "| Tempo | Evento | Dado |", "|---|---|---|"]
    rows = sp.get("timeline")
    if not rows:
        rows = []
        for k in s["events"]["kills"]:
            if c.focus["pid"] in {k["killer"], k["victim"], *k["assists"]}:
                rows.append([k["time"], kill_words(c, k), where(k).strip(" ()")])
        for o in s["events"]["objectives"]:
            if o["type"] in EPIC and o["killerTeam"]:
                rows.append([o["time"], f'{"seu time" if o["killerTeam"] == c.team else "inimigo"} pega {o["name"]}',
                             ", ".join(c.name(p) for p in o["participantsCertain"])])
        rows.sort(key=lambda r: parse_time(r[0].split("–")[0].split("-")[0]))
    L += [f"| {a} | {b} | {d} |" for a, b, d in rows] + [""]
    L += rp.md_verdicts(sp, "Perguntas e alegações")
    if getattr(c, "decisions", None):
        L += ["## Revisão de decisões", "| tempo | decisão | na hora | sinais | resultado | veredito |", "|---|---|---|---|---|---|"]
        for d in c.decisions:
            st, r = d["state"], d["result"]
            now = f'vida {st["hp"]}%, {st["bolso"]} no bolso, nível {st["nivel"]}' + (f'; lane {d["lane"]["nivel"]}' if d["lane"] else "")
            res = f'{r["abatesNossos"]}×{r["abatesDeles"]}, ouro {r["ouroTime"]:+d}' + (f'; +{", ".join(r["objetivosNossos"])}' if r["objetivosNossos"] else "") \
                + (f'; −{", ".join(r["objetivosDeles"])}' if r["objetivosDeles"] else "")
            L.append(f'| {d["time"]} | {d["what"]} | {now} | {"; ".join(d["signals"]) or "—"} | {res} | {d["verdict"]} |')
            if d.get("note"):
                L.append(f'| | ↳ {d["note"]} | | | | |')
        L.append("")
    L += rp.md_tail(sp, s["limitations"] + sp.get("limitations", []), "Limitações desta análise")
    return "\n".join(L)


# ----------------------------------------------------------------------------- main
def render(spec_path: Path, only: str | None, draft: bool = False) -> list[Path]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    lint(spec, draft)
    if spec.get("mode") == "multi":
        from render_multi import render_multi
        return render_multi(spec_path, spec, only, TEMPLATE)
    s = get_summary(int(spec["gameId"]))
    c = Ctx(spec, s)
    c.decisions = decisions_data(c)
    data = {"decisions": c.decisions,"gold": gold_data(c), "strip": strip_data(c), "pathMap": path_data(c), "lane": lane_data(c),
            "fights": [fight_data(c, f) for f in spec.get("fights", [])], "objectives": objectives_data(c),
            "replay": replay_data(c), "structures": structures_data(c),
            "icons": icons_data(c),
            "mapImg": ("data:image/png;base64," + base64.b64encode(MINIMAP.read_bytes()).decode())
                      if (s["mapId"] == 11 and MINIMAP.exists()) else None}
    outs = []
    if only in (None, "html"):
        tpl = TEMPLATE.read_text(encoding="utf-8")
        title = spec.get("pageTitle") or f'{c.focus["champion"]} × {c.opp["champion"] if c.opp else "partida"} {spec.get("tema", "")}'.strip()
        page = (tpl.replace("<!--TITLE-->", html.escape(title))
                   .replace("<!--BODY-->", html_body(c, data))
                   .replace("/*DATA*/", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
        p = spec_path.with_suffix(".html"); p.write_text(page, encoding="utf-8"); outs.append(p)
    if only in (None, "md"):
        p = spec_path.with_suffix(".md"); p.write_text(md_report(c), encoding="utf-8"); outs.append(p)
    return outs


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?", help="arquivo .json do spec (em reports/)")
    ap.add_argument("--only", choices=["html", "md"])
    ap.add_argument("--draft", action="store_true", help="aceita spec com TODO (pré-visualização; não publique)")
    ap.add_argument("--example", action="store_true", help="imprime um spec de exemplo")
    a = ap.parse_args()
    if a.example:
        print(json.dumps(EXAMPLE, ensure_ascii=False, indent=2)); return
    if not a.spec:
        ap.error("informe o spec")
    for p in render(Path(a.spec).resolve(), a.only, a.draft):
        try:
            print(p.relative_to(ROOT))
        except ValueError:
            print(p)


if __name__ == "__main__":
    main()
