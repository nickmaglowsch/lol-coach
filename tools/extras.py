"""Dados extras da API oficial (Match-V5) para relatórios e consultas.

- Ganks e lutas sem abate (ESTIMADOS): vêm de events.ganks / events.skirmishes do resumo, que usam o dano a campeões
  em cada minuto e as posições interpoladas (summarize.find_skirmishes).
- Totais da partida (CERTOS, sem horário): pings por tipo, usos de feitiços de invocador e habilidades, e uma seleção
  das estatísticas prontas da Riot (`challenges`).
Partidas só com dados do cliente (LCU) não têm nada disso: as funções devolvem listas vazias.
"""
from __future__ import annotations

import re

from summarize import CHALLENGES_PT, PING_PT

GANK_CAP = ("Estimado. Um gank entra aqui quando o jungler causou dano a campeões naquele minuto (dado da API oficial) "
            "e, pelas posições interpoladas a cada 10 s, chegou perto de um inimigo numa lane (ou no rio, junto de um "
            "laner). \"Sem local\" = houve dano, mas as posições estimadas não põem o jungler perto de ninguém. Um gank "
            "que só fez o laner recuar, sem dano, não aparece. Só até 15:00.")
TOTALS_CAP = ("Totais da partida inteira, direto da API oficial (certos, mas sem horário): não dá para saber quando cada "
              "ping ou Flash aconteceu.")
JUNGLE_ONLY = {"scuttleCrabKills", "enemyJungleMonsterKills", "jungleCsBefore10Minutes", "killsOnLanersEarlyJungleAsJungler"}
LANE_ONLY = {"laneMinionsFirst10Minutes"}
PCT = {"teamDamagePercentage"}


def _victims(kills: list[str]) -> str:
    return ", ".join(re.sub(r"\(\d+\)$", "", k) for k in kills)


def gank_rows(s: dict, name=None, until_ts: int = 900000) -> list[dict]:
    """Ganks até 15:00 dos dois junglers: [{janela, pid, time, quem, lane, dano, inimigos, resultado, abate}]."""
    name = name or (lambda pid: next(p["champion"] for p in s["participants"] if p["pid"] == pid))
    out = []
    for g in (s["events"].get("ganks") or []):
        if g["ts0"] >= until_ts:
            continue
        kf, do = g.get("killsFor"), g.get("deathsOwn")
        if kf is None:            # resumo antigo, sem a separação
            kf, do = g["kills"], []
        parts = (["abate: " + _victims(kf)] if kf else []) + (["morte: " + _victims(do)] if do else [])
        res = "; ".join(parts) or "sem abate"
        out.append({"janela": f'{g["from"]}–{g["to"]}', "pid": g["jungler"], "team": g["team"], "quem": name(g["jungler"]),
                    "lane": g["lane"] if g.get("located", True) and g["lane"] != "?" else "sem local",
                    "dano": g["dmg"], "inimigos": ", ".join(g["enemies"]) or "—", "resultado": res, "abate": bool(kf)})
    return out


def _fmt(k: str, v) -> str:
    if v is None:
        return "—"
    if k in PCT:
        return f"{100 * v:.0f}%"
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"
    return str(v)


def totals_rows(s: dict, focus: dict, opp: dict | None) -> list[list[str]]:
    """[[métrica, foco, oponente]] com pings, feitiços, habilidades e estatísticas da Riot. [] sem dados."""
    if not focus.get("pings") and not focus.get("casts") and not focus.get("challenges"):
        return []
    jungle = focus.get("role") == "JUNGLE"

    def pings(p):
        pg = (p or {}).get("pings") or {}
        tot = sum(pg.values())
        topk = sorted(((v, k) for k, v in pg.items() if v), reverse=True)[:3]
        return f"{tot}" + (f" ({', '.join(f'{PING_PT[k]} {v}' for v, k in topk)})" if topk else "") if pg else "—"

    def spell(p, sid):
        c = (p or {}).get("casts") or {}
        return next((str(x["casts"]) for x in c.get("summoners", []) if x["id"] == sid), "—")

    def abil(p):
        c = (p or {}).get("casts") or {}
        return " · ".join(f"{k} {c[k]}" for k in ("Q", "W", "E", "R") if c.get(k) is not None) or "—"

    rows = [["Pings (total e mais usados)", pings(focus), pings(opp)],
            ["Flash usado", spell(focus, 4), spell(opp, 4)],
            ["Habilidades usadas", abil(focus), abil(opp)]]
    if jungle:
        rows.append(["Smite usado", spell(focus, 11), spell(opp, 11)])
    ch_f, ch_o = focus.get("challenges") or {}, (opp or {}).get("challenges") or {}
    for k, label in CHALLENGES_PT.items():
        if (k in JUNGLE_ONLY and not jungle) or (k in LANE_ONLY and jungle):
            continue
        if k in ch_f or k in ch_o:
            rows.append([label, _fmt(k, ch_f.get(k)), _fmt(k, ch_o.get(k))])
    return rows


# ----------------------------------------------------------------------------- HTML / Markdown do relatório
def ganks_html(rows: list[dict], side, E) -> str:
    if not rows:
        return ""
    body = "".join(f'<tr><td>{E(r["janela"])}</td><td class="{"ally" if side(r["team"]) == "a" else "enemy"}">{E(r["quem"])}</td>'
                   f'<td>{E(r["lane"])}</td><td>{r["dano"]}</td><td>{E(r["inimigos"])}</td><td>{E(r["resultado"])}</td></tr>'
                   for r in rows)
    n_fail = sum(1 for r in rows if not r["abate"])   # sem abate do time do jungler
    return (f'<section><div class="eyebrow">Ganks e lutas sem abate</div><h2>Ganks até 15:00: {len(rows)} '
            f'({n_fail} sem abate)</h2><div class="fig"><div class="scroll"><table class="tbl"><thead><tr><th>minuto</th>'
            f'<th>jungler</th><th>lane</th><th>dano a campeões</th><th>inimigos perto (est.)</th><th>resultado</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div><p class="cap">{E(GANK_CAP)}</p></div></section>')


def ganks_md(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    L = ["## Ganks e lutas sem abate (estimado)", "| Minuto | Jungler | Lane | Dano a campeões | Inimigos perto (est.) | Resultado |",
         "|---|---|---|---|---|---|"]
    L += [f'| {r["janela"]} | {r["quem"]} | {r["lane"]} | {r["dano"]} | {r["inimigos"]} | {r["resultado"]} |' for r in rows]
    return L + ["", GANK_CAP, ""]


def totals_html(rows: list[list[str]], who: str, opp: str | None, E) -> str:
    if not rows:
        return ""
    body = "".join(f"<tr><td>{E(a)}</td><td>{E(b)}</td><td>{E(c)}</td></tr>" for a, b, c in rows)
    return (f'<section><div class="eyebrow">API oficial</div><h2>Totais da partida: {E(who)}'
            f'{" × " + E(opp) if opp else ""}</h2><div class="fig"><div class="scroll"><table class="tbl"><thead><tr><th></th>'
            f'<th>{E(who)}</th><th>{E(opp or "oponente")}</th></tr></thead><tbody>{body}</tbody></table></div>'
            f'<p class="cap">{E(TOTALS_CAP)}</p></div></section>')


def totals_md(rows: list[list[str]], who: str, opp: str | None) -> list[str]:
    if not rows:
        return []
    L = [f"## Totais da partida (API oficial): {who}" + (f" × {opp}" if opp else ""), f"| | {who} | {opp or 'oponente'} |", "|---|---|---|"]
    return L + [f"| {a} | {b} | {c} |" for a, b, c in rows] + ["", TOTALS_CAP, ""]
