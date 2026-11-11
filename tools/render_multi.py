"""Relatório de VÁRIAS partidas (padrões, maré de derrotas, vitórias × derrotas).

Spec com "mode": "multi". Campos próprios:
  "focus": "me", "alias": "Nick", "ranked": true | "queue": 440, "n": 0 (0 = todas em cache),
  "title", "pageTitle", "note", "tldr", "verdicts", "sections", "recs", "limitations", "artifactUrl"
  "highlight": {"<gameId>": "rótulo curto"}  -> partidas destacadas no gráfico de ouro
Os números (tabela, curvas de ouro, mortes por minuto) vêm de `lol games` / resumos; o spec só traz o julgamento.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import report_parts as rp
from query import EPIC_COUNT, my_games
from report_parts import mk
from summarize import ROLE_PT


def collect(spec: dict, flt: dict | None = None) -> list[dict]:
    """Partidas do jogador; `flt` (since/until/role/result/queue/ranked) sobrescreve os filtros do spec."""
    f = dict(spec, **(flt or {}))
    args = SimpleNamespace(queue=f.get("queue"), ranked=f.get("ranked", False), n=f.get("n", 0),
                           player=spec.get("focus", "me"), since=f.get("since"), until=f.get("until"))
    out = []
    for s, p in my_games(args):
        if f.get("role") and ROLE_PT.get(p.get("role", ""), "?") != f["role"]:
            continue
        if f.get("result") and p["stats"]["win"] != (f["result"] == "win"):
            continue
        team = p["team"]
        sign = -1 if team == 200 else 1
        series = [sign * r["teamDiff100"]["gold"] for r in s["perMinute"]]
        drag = Counter()
        for o in s["events"]["objectives"]:
            if o["type"] == "DRAGON" and o["killerTeam"] in (100, 200) and "ancião" not in o["name"]:
                drag["a" if o["killerTeam"] == team else "e"] += 1
        deaths = [k for k in s["events"]["kills"] if k["victim"] == p["pid"]]
        ag, st = p["aggregates"], p["stats"]
        out.append({
            "gameId": s["gameId"], "date": s["date"], "queue": s["queue"], "champion": p["champion"],
            "role": ROLE_PT.get(p.get("role", ""), "?"), "win": st["win"], "min": round(s["duration"] / 60),
            "kda": f'{st["kills"]}/{st["deaths"]}/{st["assists"]}', "deaths": st["deaths"],
            "deathMin": [k["ts"] // 60000 for k in deaths],
            "alone": sum(1 for k in deaths if not k["alliesOfVictimNearEst"]),
            "gd15": ag["goldDiff15"], "team15": series[15] if len(series) > 15 else None,
            "teamMax": max(series), "teamMaxMin": series.index(max(series)),
            "dragA": drag["a"], "dragE": drag["e"], "series": series,
            "queueId": s["queueId"], "zoneShare": ag["zoneShare"], "minutes": len(s["perMinute"]) - 1,
            "deathsOwnJungle": sum(1 for k in deaths if k["zoneForVictim"] == "jungle_ally"),
            "drag20": (sum(1 for o in s["events"]["objectives"] if o["type"] == "DRAGON" and o["ts"] < 1200000 and o["killerTeam"] == team),
                       sum(1 for o in s["events"]["objectives"] if o["type"] == "DRAGON" and o["ts"] < 1200000 and o["killerTeam"] in (100, 200) and o["killerTeam"] != team)),
        })
    return out


def group_metrics(games: list[dict]) -> dict:
    """Métricas comparáveis de um grupo de partidas (médias por partida; zonas ponderadas pelos minutos)."""
    n = len(games) or 1
    mins = sum(g["minutes"] for g in games) or 1
    dec = lambda v: f"{v:.1f}".replace(".", ",")
    sgn = lambda v: f"{v:+,.0f}".replace(",", ".").replace("-", "−")
    zone = lambda z: sum(g["zoneShare"].get(z, 0) * g["minutes"] for g in games) / mins
    champs = Counter(g["champion"] for g in games).most_common(1)
    losses = [g for g in games if not g["win"]]
    avg = lambda vals: (sum(vals) / len(vals)) if vals else None
    return {
        "Partidas": len(games),
        "Vitórias": f'{sum(g["win"] for g in games)} ({100 * sum(g["win"] for g in games) / n:.0f}%)',
        "Campeão mais jogado": f'{champs[0][0]} ({champs[0][1]})' if champs else "—",
        "Partidas na Solo/Duo": f'{100 * sum(1 for g in games if g["queueId"] == 420) / n:.0f}%',
        "Mortes por partida": dec(avg([g["deaths"] for g in games])),
        "Mortes na própria jungle por partida": dec(avg([g["deathsOwnJungle"] for g in games])),
        "Mortes sem aliado perto por partida (est.)": dec(avg([g["alone"] for g in games])),
        "Tempo na jungle inimiga": f'{100 * zone("jungle_enemy"):.0f}%',
        "Tempo no rio": f'{100 * zone("river"):.0f}%',
        "Tempo na própria jungle": f'{100 * zone("jungle_ally"):.0f}%',
        "Sua dif. de ouro aos 15 (vs oponente)": sgn(avg([g["gd15"] for g in games if g["gd15"] is not None])),
        "Ouro do time aos 15 (média)": sgn(avg([g["team15"] for g in games if g["team15"] is not None])),
        "Time atrás aos 15": f'{100 * sum(1 for g in games if (g["team15"] or 0) < 0) / n:.0f}%',
        "Atrás em dragões aos 20": f'{100 * sum(1 for g in games if g["drag20"][0] < g["drag20"][1]) / n:.0f}%',
        "Derrotas que tiveram +2.500": (f'{sum(1 for g in losses if g["teamMax"] >= 2500)} de {len(losses)}' if losses else "—"),
    }


def render_multi(spec_path: Path, spec: dict, only: str | None, template: Path) -> list[Path]:
    games = collect(spec)
    if not games:
        raise SystemExit("Nenhuma partida em cache para esse filtro.")
    E = lambda x: html.escape(str(x))
    alias = spec.get("alias", "você")
    W = sum(g["win"] for g in games); L = len(games) - W
    losses = [g for g in games if not g["win"]]
    behind15 = sum(1 for g in losses if g["team15"] is not None and g["team15"] < 0)
    soul = sum(1 for g in losses if g["dragE"] >= 4)
    thrown = sum(1 for g in losses if g["teamMax"] >= 2500)
    deaths_pg = sum(g["deaths"] for g in games) / len(games)
    first = dt.datetime.fromisoformat(games[-1]["date"]); last = dt.datetime.fromisoformat(games[0]["date"])
    fila = "ranqueadas" if spec.get("ranked") else (games[0]["queue"] if spec.get("queue") else "todas as filas")
    buckets = Counter()
    for g in games:
        for m in g["deathMin"]:
            buckets[min(m // 5, 9)] += 1
    hl = {str(k): v for k, v in spec.get("highlight", {}).items()}
    data = {"multi": {"series": [{"id": g["gameId"], "win": g["win"], "pts": g["series"], "hl": hl.get(str(g["gameId"]))} for g in games],
                      "deaths": [buckets.get(i, 0) for i in range(10)]}}

    tiles = [(f'<b class="{"ally" if W >= L else "enemy"}">{W}V – {L}D</b>', f'{len(games)} partidas {fila}, {first:%d/%m} a {last:%d/%m}'),
             (f'<b>{deaths_pg:.1f}</b>'.replace('.', ','), f'mortes de {E(alias)} por partida'),
             (f'<b class="enemy">{behind15}/{L}</b>', 'derrotas com o time atrás no ouro aos 15 min'),
             (f'<b class="enemy">{soul}/{L}</b>', 'derrotas em que o inimigo fez 4+ dragões'),
             (f'<b class="enemy">{thrown}/{L}</b>', 'derrotas em que o time chegou a +2.500 de ouro')]
    out = [rp.header(f"Padrões em várias partidas · {alias}", spec.get("title", "Padrões nas últimas partidas"), tiles,
                     "Cada conclusão vem da timeline das partidas (posição por minuto, kills e objetivos) e "
                     + rp.VERDICT_LEGEND + ". Amostras pequenas são sinalizadas."),
           rp.note(spec), rp.tldr(spec)]
    if spec.get("compare"):
        # cada grupo usa o histórico inteiro (só herda focus/ranked/queue/role do spec), não o recorte da visão principal
        base = {k: spec[k] for k in ("focus", "ranked", "queue", "role") if k in spec}
        groups = [(g["label"], group_metrics(collect(base, {k: v for k, v in g.items() if k != "label"}))) for g in spec["compare"]]
        keys = list(groups[0][1].keys())
        head = "".join(f"<th>{E(lbl)}</th>" for lbl, _ in groups)
        body = "".join(f'<tr><td>{E(k)}</td>' + "".join(f'<td class="n">{E(m[k])}</td>' for _, m in groups) + "</tr>" for k in keys)
        out.append(f'''<section><div class="eyebrow">Comparação</div><h2>{mk(spec.get("compareTitle", "O que muda entre os grupos"))}</h2>
<div class="fig"><div class="scroll"><table class="tbl cmp"><thead><tr><th>métrica</th>{head}</tr></thead><tbody>{body}</tbody></table></div>
<p class="cap">{mk(spec.get("compareCaption", "Mesmas métricas para cada grupo de partidas. Tempo por zona é ponderado pela duração das partidas."))}</p></div></section>''')
        data["compare"] = [{"label": lbl, "metrics": m} for lbl, m in groups]
    out.append('''<section><div class="eyebrow">Ouro do time, partida por partida</div><h2>Como as partidas andaram</h2>
<div class="fig"><div class="legend"><span><i style="background:var(--ally)"></i>vitória</span><span><i style="background:var(--enemy)"></i>derrota</span><span><i style="background:var(--gold)"></i>destaque</span></div>
<div class="scroll"><svg id="multiGold" viewBox="0 0 820 320" role="img" aria-label="Diferença de ouro do time por minuto em cada partida"></svg></div>
<p class="cap">Cada linha é uma partida (ouro do seu time − inimigo). Linhas vermelhas acima de zero que terminam embaixo são vantagens que não viraram vitória.</p></div></section>''')
    out.append(rp.verdicts(spec, "Perguntas", "O que os dados dizem"))
    out.append(f'''<section><div class="eyebrow">Mortes de {E(alias)}</div><h2>Em que minuto você morre</h2>
<div class="fig"><svg id="deathBars" viewBox="0 0 820 220" role="img" aria-label="Mortes por faixa de 5 minutos"></svg>
<p class="cap">Total de mortes nas {len(games)} partidas, por faixa de 5 minutos. Estimadas sem aliado perto: {sum(g["alone"] for g in games)} de {sum(g["deaths"] for g in games)}.</p></div></section>''')
    def signed(v):
        return "" if v is None else f"{v:+,d}".replace(",", ".").replace("-", "−")

    def row(g):
        when = dt.datetime.fromisoformat(g["date"]).strftime("%d/%m %H:%M")
        cells = [when, E(g["queue"].replace("Ranked ", "")), E(g["champion"]), E(g["role"]),
                 "<b>" + ("V" if g["win"] else "D") + "</b>", f'{g["min"]}′', E(g["kda"])]
        nums = [signed(g["gd15"]), signed(g["team15"]), f'{signed(g["teamMax"])} ({g["teamMaxMin"]}′)', f'{g["dragA"]}–{g["dragE"]}']
        tds = "".join(f"<td>{c}</td>" for c in cells) + "".join(f'<td class="n">{c}</td>' for c in nums)
        return f'<tr class="{"w" if g["win"] else "l"}">{tds}<td class="mono">{g["gameId"]}</td></tr>'

    rows = "".join(row(g) for g in games)
    out.append(f'''<section><div class="eyebrow">Partida por partida</div><h2>Tabela</h2>
<div class="fig"><div class="scroll"><table class="tbl"><thead><tr><th>data</th><th>fila</th><th>campeão</th><th>role</th><th>res</th><th>dur</th><th>KDA</th><th>dif. ouro @15 (você)</th><th>ouro do time @15</th><th>maior vantagem (min)</th><th>dragões</th><th>jogo</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="cap">"dif. ouro @15 (você)" é contra o oponente direto da sua role; dragões = seu time–inimigo (sem o ancião).</p></div></section>''')
    out += rp.sections(spec)
    out.append(rp.recs(spec, "Para as próximas"))
    lim = ["A posição de cada jogador só é registrada uma vez por minuto; ganks sem kill, Flash, pings e voz não aparecem.",
           "O cliente só guarda as 20 partidas mais recentes; amostras pequenas (especialmente de vitórias) são sinalizadas nos vereditos.",
           "Roles e recalls são inferidos; 'sem aliado perto' é estimado."] + spec.get("limitations", [])
    out.append(rp.limits(lim))
    out.append('<footer>Gerado pelo lol-coach a partir das timelines das partidas em cache. Partidas fora de Summoner\'s Rift e remakes ficam de fora.</footer>')

    outs = []
    if only in (None, "html"):
        tpl = template.read_text(encoding="utf-8")
        page = (tpl.replace("<!--TITLE-->", html.escape(spec.get("pageTitle", "Padrões das partidas")))
                   .replace("<!--BODY-->", "\n".join(x for x in out if x))
                   .replace("/*DATA*/", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
        p = spec_path.with_suffix(".html"); p.write_text(page, encoding="utf-8"); outs.append(p)
    if only in (None, "md"):
        md = [f'# {spec.get("title", "Padrões nas últimas partidas")}', "",
              f'{len(games)} partidas {fila} ({first:%d/%m} a {last:%d/%m}): **{W}V – {L}D** · {str(round(deaths_pg, 1)).replace(".", ",")} mortes/partida', ""]
        md += rp.md_intro(spec)
        if data.get("compare"):
            labels = [g["label"] for g in data["compare"]]
            md += ["## Comparação", "| métrica | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
            for k in data["compare"][0]["metrics"]:
                md.append(f"| {k} | " + " | ".join(str(g["metrics"][k]) for g in data["compare"]) + " |")
            md.append("")
        md += ["## Partidas", "| data | fila | campeão | role | res | dur | KDA | dif @15 | time @15 | maior vantagem | dragões | jogo |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        md += [f'| {g["date"][5:16].replace("T", " ")} | {g["queue"]} | {g["champion"]} | {g["role"]} | {"V" if g["win"] else "D"} | {g["min"]} | {g["kda"]} | '
               f'{g["gd15"] if g["gd15"] is not None else ""} | {g["team15"] if g["team15"] is not None else ""} | {g["teamMax"]} ({g["teamMaxMin"]}′) | {g["dragA"]}–{g["dragE"]} | {g["gameId"]} |'
               for g in games] + [""]
        md += rp.md_verdicts(spec, "Perguntas")
        md += rp.md_tail(spec, lim, "Limitações")
        p = spec_path.with_suffix(".md"); p.write_text("\n".join(md), encoding="utf-8"); outs.append(p)
    return outs
