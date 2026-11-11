"""Painel de evolução: uma página que junta todas as partidas em cache do jogador "me".

  python tools/dashboard.py [--ranked] [--queue Q] [--role jungle] [--since AAAA-MM-DD] [--until AAAA-MM-DD]
                            [--recent 10] [--window 5] [--alias Nick] [--out reports/painel.html]

Mostra: placar geral e das últimas 10/20; série partida a partida (vitória, mortes, saldo de dragões aos 20:00,
tempo na jungle inimiga, dif. de ouro aos 15:00 contra o oponente, decisões "ruim" de `lol decisions`) com média
móvel; as últimas N partidas comparadas com as metas das recomendações (METAS, abaixo); relatórios publicados
(reports/*.json) e partidas recentes sem relatório.
Os números vêm dos resumos (`render_multi.collect`, o mesmo usado nos relatórios de várias partidas) e de
`decisions.review`; nada é escrito à mão. Gerar de novo é só rodar o comando: o cache novo entra sozinho.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

from common import REPORTS, load_json, resolve_player
from decisions import infer_timers, review
from query import all_summaries
from render_multi import collect, group_metrics
from report_parts import E, header, limits, mk
from scaffold import me_alias
from summarize import get_summary

TOOLS = Path(__file__).resolve().parent
TEMPLATE = TOOLS / "templates" / "dashboard.html"

# ----------------------------------------------------------------------------- metas (edite aqui)
# Metas atuais das recomendações publicadas. "fonte" = nome do spec em reports/ (sem .json), para linkar o relatório.
# op: ">=", ">", "<=", "<". "key" é uma métrica por partida (ver `game_row`).
METAS = {
    "recentes": 10,         # quantas partidas recentes comparar com as metas
    "mediaMovel": 5,        # janela da média móvel dos gráficos
    "amostraPequena": 10,   # abaixo disso o grupo é marcado como amostra pequena
    "campeao": {"nome": "Master Yi", "fatiaMin": 0.5, "titulo": "Voltar ao Master Yi",
                "texto": "pelo menos metade das partidas recentes com ele",
                "fonte": "2026-11-11-multi-antes-e-agora"},
    "metricas": [
        {"key": "enemyJg", "titulo": "Tempo na jungle inimiga", "op": ">=", "alvo": 8, "unid": "%",
         "fonte": "2026-11-11-multi-antes-e-agora"},
        {"key": "dragDiff20", "titulo": "Não ficar atrás em dragões aos 20:00", "op": ">=", "alvo": 0, "unid": "",
         "fonte": "2026-11-11-multi-antes-e-agora"},
        {"key": "deaths", "titulo": "Mortes por partida", "op": "<=", "alvo": 4, "unid": "",
         "fonte": "2026-11-11-multi-mare-de-derrotas"},
        {"key": "gd15", "titulo": "Vantagem sobre o oponente aos 15:00", "op": ">", "alvo": 0, "unid": "",
         "fonte": "2026-11-11-multi-antes-e-agora"},
    ],
}

# gráficos da série (a meta de cada um vem de METAS["metricas"] pela key)
CHARTS = [
    {"key": "win", "title": "Vitórias", "kind": "win"},
    {"key": "deaths", "title": "Mortes"},
    {"key": "dragDiff20", "title": "Saldo de dragões aos 20:00 (seu time − inimigo)"},
    {"key": "enemyJg", "title": "Tempo na jungle inimiga (% da partida)", "unit": "%"},
    {"key": "gd15", "title": "Dif. de ouro aos 15:00 contra o oponente direto", "signed": True},
    {"key": "bad", "title": "Decisões com veredito \"ruim\" (lol decisions)"},
]

OPS = {">=": lambda v, a: v >= a, ">": lambda v, a: v > a, "<=": lambda v, a: v <= a, "<": lambda v, a: v < a}
OP_TXT = {">=": "≥", ">": ">", "<=": "≤", "<": "<"}


# ----------------------------------------------------------------------------- formatação pt-BR
def num(v, dec: int = 0, signed: bool = False, unit: str = "") -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "−" if v < 0 and s.strip("0,.") else ("+" if signed and v > 0 else "")
    return sign + s + unit


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


# ----------------------------------------------------------------------------- dados
def game_row(g: dict, timers: dict) -> dict:
    """Uma partida (de render_multi.collect) + decisões de decisions.review."""
    s = get_summary(g["gameId"])
    p = resolve_player(s["participants"], "me")
    decs = review(s, p, timers) if p else []
    a, e = g["drag20"]
    return {"id": g["gameId"], "date": g["date"], "champ": g["champion"], "role": g["role"],
            "queue": g["queue"].replace("Ranked ", ""), "win": g["win"], "min": g["min"], "kda": g["kda"],
            "deaths": g["deaths"], "dragA20": a, "dragE20": e, "dragDiff20": a - e,
            "enemyJg": round(100 * g["zoneShare"].get("jungle_enemy", 0), 1), "minutes": g["minutes"],
            "gd15": g["gd15"], "bad": sum(1 for d in decs if d["verdict"] == "ruim"), "dec": len(decs)}


def wl(games: list[dict]) -> tuple[int, int]:
    w = sum(1 for g in games if g["win"])
    return w, len(games) - w


def metric_value(games: list[dict], key: str):
    """Valor do grupo: tempo na jungle inimiga ponderado pelos minutos (como nos relatórios); o resto, média por partida."""
    if key == "enemyJg":
        mins = sum(g["minutes"] for g in games)
        return sum(g["enemyJg"] * g["minutes"] for g in games) / mins if mins else None
    return avg([g[key] for g in games])


def load_reports() -> list[dict]:
    out = []
    for f in sorted(REPORTS.glob("*.json")):
        try:
            sp = load_json(f)
        except (OSError, ValueError):
            continue
        if not isinstance(sp, dict) or not (sp.get("gameId") or sp.get("mode") == "multi"):
            continue
        text = json.dumps(sp, ensure_ascii=False)
        out.append({"stem": f.stem, "date": f.stem[:10], "title": sp.get("title") or sp.get("pageTitle") or f.stem,
                    "gameId": sp.get("gameId"), "multi": sp.get("mode") == "multi", "url": sp.get("artifactUrl"),
                    "draft": "TODO" in text})
    out.sort(key=lambda r: (r["date"], r["stem"]), reverse=True)
    return out


# ----------------------------------------------------------------------------- blocos HTML
def chip(ok: bool | None, small: bool) -> str:
    if ok is None:
        return '<span class="chip unk">sem dados</span>'
    txt = "na meta" if ok else "fora da meta"
    extra = ' <span class="chip unk">amostra pequena</span>' if small else ""
    return f'<span class="chip {"ok" if ok else "bad"}">{txt}</span>{extra}'


def src_link(stem: str | None, reps: dict) -> str:
    r = reps.get(stem or "")
    if not r:
        return ""
    if r["url"]:
        return f'<a href="{E(r["url"])}" target="_blank" rel="noopener">{E(r["title"])}</a>'
    return E(r["title"])


def metas_block(games_new_first: list[dict], reps: dict) -> tuple[str, int, int]:
    N, small_n = METAS["recentes"], METAS["amostraPequena"]
    recent, before = games_new_first[:N], games_new_first[N:]
    small = len(recent) < min(N, small_n)
    cards, met, total = [], 0, 0

    c = METAS.get("campeao")
    if c:
        mine = [g for g in recent if g["champ"] == c["nome"]]
        others = [g for g in recent if g["champ"] != c["nome"]]
        hist = [g for g in before if g["champ"] == c["nome"]]
        share = len(mine) / len(recent) if recent else 0
        ok = share >= c["fatiaMin"] if recent else None
        met += bool(ok); total += 1
        w, _ = wl(mine); wo, _ = wl(others); wh, _ = wl(hist)
        tiny = lambda k: ' <span class="small">(amostra pequena)</span>' if 0 < k < 5 else ""
        lines = [f'Vitórias com ele nas últimas {len(recent)}: <b>{w} de {len(mine)}</b>{tiny(len(mine))}' if mine
                 else "Nenhuma partida com ele nas últimas " + str(len(recent)) + ".",
                 f'Com outros campeões: {wo} de {len(others)}{tiny(len(others))}' if others else "",
                 f'Antes dessas: {wh} de {len(hist)} ({pct(wh, len(hist))}) com ele' if hist else ""]
        cards.append(f'''<article class="meta"><div class="meta-head"><h3>{E(c["titulo"])}</h3>{chip(ok, small)}</div>
<div class="meta-val"><b>{len(mine)}<small>/{len(recent)}</small></b><span>partidas de {E(c["nome"])} · meta: {E(c["texto"])}</span></div>
<ul>{"".join("<li>" + l + "</li>" for l in lines if l)}</ul>
<p class="src">{src_link(c.get("fonte"), reps)}</p></article>''')

    for m in METAS["metricas"]:
        key, op, alvo, unit = m["key"], m["op"], m["alvo"], m.get("unid", "")
        dec = 1 if key == "enemyJg" else (0 if key == "gd15" else 1)
        sg = key in ("gd15", "dragDiff20")
        vals = [g[key] for g in recent if g[key] is not None]
        v, vb = metric_value(recent, key), metric_value(before, key)
        ok = OPS[op](v, alvo) if v is not None else None
        met += bool(ok); total += 1
        hits = sum(1 for x in vals if OPS[op](x, alvo))
        trend = ""
        if v is not None and vb is not None:
            better = OPS[op](v, vb)   # mesmo sentido da meta: ">=" quer subir, "<=" quer descer
            trend = (f'Antes dessas ({len(before)} partidas): {num(vb, dec, sg, unit)} — '
                     + ("melhor agora" if (better and v != vb) else "igual" if v == vb else "pior agora"))
        cards.append(f'''<article class="meta"><div class="meta-head"><h3>{E(m["titulo"])}</h3>{chip(ok, small)}</div>
<div class="meta-val"><b>{num(v, dec, sg, unit)}</b><span>média das últimas {len(recent)} · meta: {OP_TXT[op]} {num(alvo, 0, sg, unit)}</span></div>
<ul><li>Partidas dentro da meta: <b>{hits} de {len(vals)}</b></li>{"<li>" + trend + "</li>" if trend else ""}</ul>
<p class="src">{src_link(m.get("fonte"), reps)}</p></article>''')

    note = (f'<p class="cap">Últimas {len(recent)} partidas do filtro (de {dt.datetime.fromisoformat(recent[-1]["date"]):%d/%m/%Y} '
            f'a {dt.datetime.fromisoformat(recent[0]["date"]):%d/%m/%Y}). "Na meta" compara a média do grupo com o alvo; '
            'a linha de baixo conta partida por partida. Tempo na jungle inimiga é ponderado pela duração (como nos relatórios). '
            'Metas configuráveis em <code>METAS</code>, no topo de <code>tools/dashboard.py</code>.</p>') if recent else ""
    html_ = (f'<section><div class="eyebrow">Metas das recomendações</div><h2>Últimas {len(recent)} partidas × metas atuais</h2>'
             f'<div class="metas">{"".join(cards)}</div>{note}</section>')
    return html_, met, total


def compare_table(games_new_first: list[dict]) -> str:
    """Mesmas métricas dos relatórios (render_multi.group_metrics) para últimas 10 × 10 anteriores × histórico."""
    raw = collect_cache["raw"]
    by_id = {g["gameId"]: g for g in raw}
    groups = [("Últimas 10", games_new_first[:10]), ("10 anteriores", games_new_first[10:20]), ("Histórico (filtro)", games_new_first)]
    groups = [(lbl, gs) for lbl, gs in groups if gs]
    mets = [group_metrics([by_id[g["id"]] for g in gs]) for _, gs in groups]
    skip = {"Partidas na Solo/Duo", "Mortes na própria jungle por partida", "Tempo no rio", "Tempo na própria jungle"}
    keys = [k for k in mets[0] if k not in skip]
    rows = [(k, [m[k] for m in mets]) for k in keys]
    c = METAS.get("campeao", {}).get("nome")
    if c:
        rows.append((f"Partidas de {c}", [f'{sum(1 for g in gs if g["champ"] == c)} de {len(gs)}' for _, gs in groups]))
    rows.append(('Decisões "ruim" por partida', [num(avg([g["bad"] for g in gs]), 1) for _, gs in groups]))
    rows.append(('Decisões "ruim" (% das decisões)',
                 [pct(sum(g["bad"] for g in gs), sum(g["dec"] for g in gs)) for _, gs in groups]))
    head = "".join(f"<th>{E(l)}{' <span class=small>(amostra pequena)</span>' if len(gs) < METAS['amostraPequena'] else ''}</th>"
                   for l, gs in groups)
    body = "".join(f'<tr><td>{E(k)}</td>' + "".join(f'<td class="n">{E(v)}</td>' for v in vals) + "</tr>" for k, vals in rows)
    return (f'<div class="fig"><h3>Últimas 10 × 10 anteriores × histórico</h3><div class="scroll"><table class="tbl cmp"><thead><tr><th>métrica</th>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div><p class="cap">Mesmas definições dos relatórios de várias partidas. '
            '"Derrotas que tiveram +2.500" = derrotas em que o time chegou a +2.500 de ouro em algum minuto.</p></div>')


collect_cache: dict = {}


def reports_block(reps: list[dict]) -> str:
    if not reps:
        return '<section><div class="eyebrow">Relatórios</div><h2>Relatórios publicados</h2><p class="cap">Nenhum spec em reports/.</p></section>'
    items = []
    for r in reps:
        kind = "várias partidas" if r["multi"] else f'partida {r["gameId"]}'
        when = dt.date.fromisoformat(r["date"]).strftime("%d/%m/%Y") if r["date"][:4].isdigit() else ""
        if r["url"]:
            link = f'<a href="{E(r["url"])}" target="_blank" rel="noopener">{E(r["title"])}</a>'
        else:
            link = f'{E(r["title"])} <span class="chip unk">{"rascunho" if r["draft"] else "não publicado"}</span>'
        items.append(f'<li><span class="mono">{when}</span><div>{link}<span class="small">{E(kind)} · <code>{E(r["stem"])}</code></span></div></li>')
    return (f'<section><div class="eyebrow">Relatórios</div><h2>Relatórios publicados ({sum(1 for r in reps if r["url"])})</h2>'
            f'<ul class="reps">{"".join(items)}</ul></section>')


def recent_block(games_new_first: list[dict], reps: list[dict], n: int = 10) -> str:
    by_gid = {}
    for r in reps:
        if r["gameId"]:
            by_gid.setdefault(int(r["gameId"]), r)
    rows, missing = [], 0
    for g in games_new_first[:n]:
        r = by_gid.get(g["id"])
        if r and r["url"]:
            rep = f'<a href="{E(r["url"])}" target="_blank" rel="noopener">abrir</a>'
        elif r:
            rep = "não publicado"
        else:
            missing += 1
            rep = f'<code>lol new {g["id"]}</code>'
        when = dt.datetime.fromisoformat(g["date"]).strftime("%d/%m %H:%M")
        res = f'<b class="{"ally" if g["win"] else "enemy"}">{"V" if g["win"] else "D"}</b>'
        rows.append(f'<tr class="{"" if r else "nr"}"><td>{when}</td><td>{E(g["queue"])}</td><td>{E(g["champ"])}</td><td>{E(g["role"])}</td>'
                    f'<td>{res}</td><td>{E(g["kda"])}</td><td class="mono">{g["id"]}</td><td>{rep}</td></tr>')
    return (f'<section><div class="eyebrow">Partidas recentes</div><h2>{missing} das últimas {min(n, len(games_new_first))} sem relatório</h2>'
            f'<div class="fig"><div class="scroll"><table class="tbl"><thead><tr><th>data</th><th>fila</th><th>campeão</th><th>role</th>'
            f'<th>res</th><th>KDA</th><th>jogo</th><th>relatório</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="cap">Relatórios de várias partidas não contam aqui. <code>lol new &lt;id&gt;</code> cria o esqueleto do spec de uma partida.</p></div></section>')


# ----------------------------------------------------------------------------- página
def build(a) -> Path:
    spec = {"focus": "me", "ranked": a.ranked, "queue": a.queue, "n": 0, "since": a.since, "until": a.until, "role": a.role}
    raw = collect(spec)
    if not raw:
        raise SystemExit("Nenhuma partida em cache para esse filtro.")
    collect_cache["raw"] = raw
    timers = infer_timers(all_summaries())   # uma vez só, para todas as partidas (como `lol decisions`)
    new_first = [game_row(g, timers) for g in raw]
    games = list(reversed(new_first))       # mais antiga -> mais recente, para os gráficos
    reps = load_reports()
    reps_by_stem = {r["stem"]: r for r in reps}
    alias = a.alias or me_alias() or "você"
    small_n = METAS["amostraPequena"]

    def score(gs):
        w, l = wl(gs)
        return w, l, pct(w, len(gs))

    W, L, P = score(new_first)
    first, last = dt.datetime.fromisoformat(games[0]["date"]), dt.datetime.fromisoformat(games[-1]["date"])
    tiles = [(f'<b class="{"ally" if W >= L else "enemy"}">{W}V – {L}D</b>', f'{P} em {len(games)} partidas ({first:%d/%m/%y} a {last:%d/%m/%y})')]
    for k in (20, 10):
        if len(new_first) >= k:
            w, l, p = score(new_first[:k])
            tiles.append((f'<b class="{"ally" if w >= l else "enemy"}">{w}V – {l}D</b>', f'{p} nas últimas {k}'))
    metas_html, met, total = metas_block(new_first, reps_by_stem)
    tiles.append((f'<b class="{"good" if met == total else "enemy"}">{met}/{total}</b>', f'metas cumpridas nas últimas {min(METAS["recentes"], len(new_first))}'))
    filt = [x for x in (("ranqueadas" if a.ranked else ""), (f"fila {a.queue}" if a.queue else ""), (f"role {a.role}" if a.role else ""),
                        (f"desde {a.since}" if a.since else ""), (f"até {a.until}" if a.until else "")) if x]
    sub = (f'Filtro: {E(", ".join(filt) or "todas as partidas de Summoner’s Rift em cache")}. '
           f'Gerado em {dt.datetime.now():%d/%m/%Y %H:%M} a partir das timelines em cache; rode de novo depois de baixar partidas novas.'
           + (' <strong>Amostra pequena</strong>: menos de ' + str(small_n) + ' partidas.' if len(games) < small_n else ''))

    win = METAS["mediaMovel"]
    charts = []
    goals = {m["key"]: m for m in METAS["metricas"]}
    for c in CHARTS:
        c = dict(c)
        if c["key"] in goals:
            c["goal"], c["op"] = goals[c["key"]]["alvo"], goals[c["key"]]["op"]
        charts.append(c)
    data = {"games": [{k: g[k] for k in ("id", "date", "champ", "role", "queue", "win", "min", "kda", "deaths", "dragA20",
                                          "dragE20", "dragDiff20", "enemyJg", "gd15", "bad", "dec")} for g in games],
            "window": win, "charts": charts, "metaChamp": (METAS.get("campeao") or {}).get("nome")}

    series = f'''<section><div class="eyebrow">Partida por partida</div><h2>Tendência (média móvel de {win} partidas)</h2>
<div class="fig series">
  <div class="readout" id="readout" aria-live="polite"><label class="pick"><span>Partida</span>
    <input type="range" id="pick" min="0" max="{len(games) - 1}" value="{len(games) - 1}" aria-label="Escolher partida"></label>
    <div id="readoutText" class="rt"></div></div>
  <div class="legend"><span><i class="dot" style="background:var(--ally)"></i>vitória</span><span><i class="dot" style="background:var(--enemy)"></i>derrota</span>
    <span><i style="background:var(--ink)"></i>média móvel ({win})</span><span><i class="dash"></i>meta</span></div>
  <div class="chart"><h3>Campeão</h3><svg id="champStrip" role="img" aria-label="Campeão de cada partida"></svg><div class="legend" id="champKey"></div></div>
  <div id="charts"></div>
  <p class="cap">Eixo horizontal = partidas em ordem (a mais antiga à esquerda), não tempo corrido: há períodos sem jogos. A média móvel só começa
  depois de {win} partidas. Passe o mouse, toque num ponto ou use o seletor para ver a partida. Decisões "ruim" (de <code>lol decisions</code>)
  são julgadas pelo resultado do time na janela seguinte (ouro e objetivos), não pela intenção, e crescem com a duração da partida;
  o método foi pensado para jungle.</p>
</div>
{compare_table(new_first)}
</section>'''

    out = [header(f"Painel de evolução · {alias}", "Como as partidas estão andando", tiles, sub),
           metas_html, series, reports_block(reps), recent_block(new_first, reps)]
    lim = ["A posição de cada jogador só é registrada uma vez por minuto: tempo na jungle inimiga e decisões são estimados a partir disso.",
           "Ganks que falharam sem abate, Flash, pings e voz não aparecem nos dados.",
           "Roles são inferidas; partidas de outra role (ex.: top, suporte) entram no gráfico se o filtro não tirar (use --role jungle).",
           "Dif. de ouro aos 15:00 fica vazia quando a partida não tem oponente direto identificado ou acabou antes.",
           f"Grupos com menos de {small_n} partidas são marcados como amostra pequena; a média móvel de {win} partidas oscila muito."]
    out.append(limits(lim))
    out.append('<footer>Gerado por <code>tools/dashboard.py</code> a partir das timelines em cache. Partidas fora de Summoner\'s Rift e remakes ficam de fora.</footer>')

    tpl = TEMPLATE.read_text(encoding="utf-8")
    page = (tpl.replace("<!--TITLE-->", html.escape(f"Painel · {alias}"))
               .replace("<!--BODY-->", "\n".join(x for x in out if x))
               .replace("/*DATA*/", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(page, encoding="utf-8")
    return outp


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Painel de evolução (HTML) a partir das partidas em cache.")
    ap.add_argument("--ranked", action="store_true", help="só ranqueadas (Solo/Duo e Flex)")
    ap.add_argument("--queue", type=int, help="só uma fila (ex.: 440)")
    ap.add_argument("--role", help="só uma role (jungle, top, mid, bot, suporte)")
    ap.add_argument("--since", help="AAAA-MM-DD")
    ap.add_argument("--until", help="AAAA-MM-DD")
    ap.add_argument("--recent", type=int, help=f'partidas recentes comparadas com as metas (padrão {METAS["recentes"]})')
    ap.add_argument("--window", type=int, help=f'janela da média móvel (padrão {METAS["mediaMovel"]})')
    ap.add_argument("--alias", help="nome do jogador no título (padrão: data/me.json)")
    ap.add_argument("--out", default=str(REPORTS / "painel.html"))
    a = ap.parse_args()
    if a.recent:
        METAS["recentes"] = a.recent
    if a.window:
        METAS["mediaMovel"] = a.window
    p = build(a)
    print(p)


if __name__ == "__main__":
    main()
