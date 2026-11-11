"""Visão em volta dos objetivos épicos (só partidas com fonte Match-V5, que tem eventos de ward).

Para cada objetivo épico (dragão, ancião, vastilarvas, arauto, barão, atakhan) conta, por time, as wards
colocadas e destruídas na janela [abate − N s, abate] e diz quem pegou o objetivo. "Saldo" de um time =
wards que ele colocou − wards dele que o inimigo destruiu na janela; "vantagem" = nosso saldo − saldo deles.

  python tools/vision.py <gameId> [-p me] [--before 90] [--view objectives|bands|players] [--json] [--no-header]
  python tools/vision.py --all [-p me] [--ranked] [--queue Q] [--before 90] [--margin 1]

O que conta como ward: SIGHT_WARD, YELLOW_TRINKET, BLUE_TRINKET e CONTROL_WARD (o tipo de um WARD_KILL é o tipo
da ward destruída). Ficam de fora UNDEFINED (objetos de habilidades: Warwick, Yorick, Shen, Zyra, Jhin, Bardo…)
e TEEMO_MUSHROOM. Limitações: os eventos de ward não têm posição (não dá para saber se a ward estava perto do
objetivo); wards colocadas antes da janela e ainda vivas não entram; ward destruída sem evento (expirou) não
aparece; mais visão costuma andar junto com estar na frente (correlação, não causa).
Fonte LCU não tem eventos de ward: as funções devolvem None e o CLI explica.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict

from common import mmss, resolve_player
from summarize import cached_ids, get_summary

VISION_WARDS = {"SIGHT_WARD", "YELLOW_TRINKET", "BLUE_TRINKET", "CONTROL_WARD"}
EPIC_ORDER = ("DRAGON", "ELDER", "HORDE", "RIFTHERALD", "BARON_NASHOR", "ATAKHAN")
EPIC_PT = {"DRAGON": "dragão", "ELDER": "ancião", "HORDE": "vastilarvas", "RIFTHERALD": "arauto",
           "BARON_NASHOR": "barão", "ATAKHAN": "atakhan"}
RANKED = {420, 440}
SMALL_SAMPLE = 10
GRUB_GAP_MS = 120000   # vastilarvas mortas com menos de 2 min entre si = o mesmo grupo
NO_WARDS_MSG = ("Sem eventos de ward nesta partida (fonte LCU: só totais por jogador). "
                "Baixe pela Riot API (lol fetch <id> --source riot --force) para medir visão por objetivo.")
LIMITS = ["Eventos de ward não têm posição: a janela conta todas as wards do mapa, não só as perto do objetivo.",
          "Wards colocadas antes da janela e ainda vivas não entram; wards que expiraram não geram evento.",
          "Mais visão costuma andar junto com estar na frente no jogo: é correlação, não causa."]


# ----------------------------------------------------------------------------- dados
def has_ward_events(s: dict) -> bool:
    return bool(s["events"]["wards"].get("events"))


def ward_events(s: dict) -> list[dict] | None:
    """Eventos de ward de visão (sem objetos de habilidades) com o time de quem colocou/destruiu."""
    if not has_ward_events(s):
        return None
    team_of = {p["pid"]: p["team"] for p in s["participants"]}
    out = []
    for e in s["events"]["wards"]["events"]:
        if e.get("wardType") in VISION_WARDS and e["pid"] in team_of:
            out.append(dict(e, team=team_of[e["pid"]], control=e["wardType"] == "CONTROL_WARD"))
    return out


def epic_objectives(s: dict) -> list[dict]:
    """Objetivos épicos em ordem; vastilarvas do mesmo grupo viram um item só (com o placar do grupo)."""
    items = []
    for o in sorted(s["events"]["objectives"], key=lambda o: o["ts"]):
        kind = o["type"]
        if kind == "DRAGON" and "ancião" in (o.get("name") or ""):
            kind = "ELDER"
        if kind not in EPIC_PT:
            continue
        if kind == "HORDE" and items and items[-1]["kind"] == "HORDE" and o["ts"] - items[-1]["lastTs"] <= GRUB_GAP_MS:
            g = items[-1]
            g["teams"].append(o["killerTeam"]); g["lastTs"] = o["ts"]
            continue
        items.append({"kind": kind, "ts": o["ts"], "lastTs": o["ts"], "name": o.get("name") or EPIC_PT[kind],
                      "teams": [o["killerTeam"]]})
    return items


def _took(teams: list, team: int) -> tuple[str, int | None, str]:
    """(nós|eles|dividido|?, time que pegou, placar 'a-e' para grupos)."""
    a = sum(1 for t in teams if t == team)
    e = sum(1 for t in teams if t in (100, 200) and t != team)
    split = f"{a}-{e}" if len(teams) > 1 else ""
    if a == e == 0:
        return "?", None, split
    if a > e:
        return "nós", team, split
    if e > a:
        return "eles", 300 - team, split
    return "dividido", None, split


def _bucket(edge: int, margin: int = 1) -> str:
    return "mais" if edge >= margin else ("menos" if edge <= -margin else "igual")


def _count(evs: list[dict], team: int, lo: int, hi: int) -> dict:
    c = defaultdict(int)
    for e in evs:
        if not (lo <= e["ts"] <= hi):
            continue
        side = "our" if e["team"] == team else "their"
        what = "Placed" if e["type"] == "WARD_PLACED" else "Cleared"
        c[side + what] += 1
        if e["control"]:
            c[side + what + "Control"] += 1
    return c


# ----------------------------------------------------------------------------- funções principais
def vision_around_objectives(s: dict, team: int, before_ms: int = 90000, margin: int = 1) -> dict | None:
    """Uma linha por objetivo épico com wards por time na janela [ts − before_ms, ts] e quem pegou,
    mais um agregado da partida. None se a partida não tem eventos de ward (fonte LCU)."""
    evs = ward_events(s)
    if evs is None:
        return None
    rows = []
    for o in epic_objectives(s):
        c = _count(evs, team, o["ts"] - before_ms, o["ts"])
        took, by, split = _took(o["teams"], team)
        our_net = c["ourPlaced"] - c["theirCleared"]      # colocadas por nós − nossas que eles destruíram
        their_net = c["theirPlaced"] - c["ourCleared"]
        edge = our_net - their_net
        name = o["name"] + (f" ({len(o['teams'])})" if o["kind"] == "HORDE" else "")
        rows.append({"gameId": s["gameId"], "time": mmss(o["ts"]), "ts": o["ts"], "kind": o["kind"],
                     "objective": name, "beforeS": before_ms // 1000, "took": took, "takenBy": by, "split": split,
                     "ourPlaced": c["ourPlaced"], "ourControl": c["ourPlacedControl"],
                     "theirPlaced": c["theirPlaced"], "theirControl": c["theirPlacedControl"],
                     "weCleared": c["ourCleared"], "weClearedControl": c["ourClearedControl"],
                     "theyCleared": c["theirCleared"], "theyClearedControl": c["theirClearedControl"],
                     "ourNet": our_net, "theirNet": their_net, "edge": edge, "vision": _bucket(edge, margin)})
    by_bucket = {b: {"n": 0, "nos": 0, "eles": 0} for b in ("mais", "igual", "menos")}
    for r in rows:
        b = by_bucket[r["vision"]]
        b["n"] += 1
        b["nos"] += r["took"] == "nós"
        b["eles"] += r["took"] == "eles"
    game = {"gameId": s["gameId"], "team": team, "beforeMs": before_ms, "objectives": len(rows),
            "nos": sum(r["took"] == "nós" for r in rows), "eles": sum(r["took"] == "eles" for r in rows),
            "ourPlaced": sum(r["ourPlaced"] for r in rows), "theirPlaced": sum(r["theirPlaced"] for r in rows),
            "weCleared": sum(r["weCleared"] for r in rows), "theyCleared": sum(r["theyCleared"] for r in rows),
            "meanEdge": round(sum(r["edge"] for r in rows) / len(rows), 1) if rows else None,
            "byVision": by_bucket}
    return {"rows": rows, "game": game}


def vision_summary(s: dict, team: int, band_ms: int = 300000) -> dict | None:
    """Totais de wards (colocadas/destruídas, control wards à parte) por jogador e por time em faixas de
    5 min ao longo da partida. None se a partida não tem eventos de ward (fonte LCU)."""
    evs = ward_events(s)
    if evs is None:
        return None
    zero = lambda: {"placed": 0, "control": 0, "cleared": 0, "clearedControl": 0}
    per = defaultdict(zero)
    nb = int(s["duration"] * 1000 // band_ms) + 1 if s.get("duration") else max(e["ts"] for e in evs) // band_ms + 1
    bands = [{"from": mmss(i * band_ms), "to": mmss(min((i + 1) * band_ms, (s.get("duration") or 10 ** 9) * 1000)),
              "us": zero(), "them": zero()} for i in range(nb)]
    for e in evs:
        k = ("placed", "control") if e["type"] == "WARD_PLACED" else ("cleared", "clearedControl")
        b = bands[min(e["ts"] // band_ms, nb - 1)]["us" if e["team"] == team else "them"]
        for d in (per[e["pid"]], b):
            d[k[0]] += 1
            d[k[1]] += e["control"]
    players = []
    for p in s["participants"]:
        st = p["stats"]
        players.append(dict({"pid": p["pid"], "side": "nós" if p["team"] == team else "eles",
                             "player": p["riotId"].split("#")[0], "champion": p["champion"], "role": p.get("role")},
                            **per[p["pid"]], visionScore=st.get("visionScore"),
                            controlWardsBought=st.get("controlWardsBought")))
    players.sort(key=lambda r: (r["side"] != "nós", -r["placed"]))
    teams = {}
    for side in ("us", "them"):
        t = zero()
        for b in bands:
            for k in t:
                t[k] += b[side][k]
        teams[side] = t
    return {"players": players, "bands": bands, "teams": teams}


def aggregate(rows: list[dict]) -> list[dict]:
    """Tabela tipo de objetivo × visão antes dele (mais/igual/menos): quantas vezes pegamos."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["kind"], r["vision"])].append(r)
        groups[("TODOS", r["vision"])].append(r)
    out = []
    for kind in EPIC_ORDER + ("TODOS",):
        for b in ("mais", "igual", "menos"):
            g = groups.get((kind, b), [])
            if not g:
                continue
            nos = sum(r["took"] == "nós" for r in g)
            eles = sum(r["took"] == "eles" for r in g)
            div = sum(r["took"] == "dividido" for r in g)
            known = len(g) - sum(r["took"] == "?" for r in g)
            out.append({"objective": EPIC_PT.get(kind, "todos"), "vision": b, "n": len(g), "nos": nos, "eles": eles,
                        "dividido": div, "rate": round(100 * (nos + 0.5 * div) / known) if known else None,
                        "meanEdge": round(sum(r["edge"] for r in g) / len(g), 1),
                        "games": len({r["gameId"] for r in g}), "beforeS": g[0].get("beforeS"),
                        "sample": "pequena" if len(g) < SMALL_SAMPLE else "ok"})
    return out


# ----------------------------------------------------------------------------- HTML / Markdown
_COLS_OBJ = [("time", "min", False), ("objective", "objetivo", False), ("took", "pegou", False),
             ("ourPlaced", "nossas col.", True), ("ourControl", "ctrl", True), ("theirPlaced", "deles col.", True),
             ("theirControl", "ctrl", True), ("weCleared", "destruímos", True), ("theyCleared", "destruíram", True),
             ("edge", "vantagem", True)]
_COLS_AGG = [("objective", "objetivo", False), ("vision", "visão antes", False), ("n", "casos", True),
             ("nos", "pegamos", True), ("eles", "eles", True), ("rate", "% nosso", True),
             ("meanEdge", "vantagem média", True), ("sample", "amostra", False)]


def _rows_of(rows) -> list[dict]:
    if isinstance(rows, dict):
        rows = rows.get("rows")
    return list(rows or [])


def _cols(rows: list[dict]):
    return _COLS_AGG if "rate" in rows[0] else _COLS_OBJ


def _cell(r: dict, key: str) -> str:
    v = r.get(key)
    if key == "took" and r.get("split"):
        return f'{v} {r["split"]}'
    if key == "edge" or key == "meanEdge":
        return "" if v is None else (f"+{v}" if v > 0 else str(v))
    if key == "rate":
        return "" if v is None else f"{v}%"
    return "" if v is None else str(v)


def _caption(rows: list[dict], before_s: int | None) -> str:
    before_s = before_s or rows[0].get("beforeS")
    win = f"{before_s}s" if before_s else "segundos"
    base = (f"Wards de visão (sentinela, trinkets e control) colocadas e destruídas nos {win} antes de cada abate. "
            "Vantagem = (nossas colocadas − nossas destruídas por eles) − (deles colocadas − deles destruídas por nós). ")
    if "rate" in rows[0]:
        base += f"% nosso conta objetivo dividido como meio. Amostra pequena = menos de {SMALL_SAMPLE} casos. "
    return base + " ".join(LIMITS)


def vision_html(rows, title: str = "Visão antes dos objetivos", before_s: int | None = None) -> str:
    """Seção HTML com uma tabela (linhas por objetivo ou o agregado). Vazio se não houver linhas."""
    rows = _rows_of(rows)
    if not rows:
        return ""
    E = lambda x: html.escape(str(x))
    cols = _cols(rows)
    head = "".join(f"<th>{E(lbl)}</th>" for _, lbl, _ in cols)
    body = []
    for r in rows:
        tds = []
        for key, _, num in cols:
            txt = E(_cell(r, key))
            cls = ' class="n"' if num else ""
            if key == "took":
                side = {"nós": "ally", "eles": "enemy"}.get(r["took"])
                txt = f'<span class="{side}">{txt}</span>' if side else txt
            if key == "sample" and r.get("sample") == "pequena":
                txt = f'<span class="enemy">{txt}</span>'
            tds.append(f"<td{cls}>{txt}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<section><div class="eyebrow">Visão</div><h2>{E(title)}</h2>'
            f'<div class="fig"><div class="scroll"><table class="tbl"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>'
            f'<p class="cap">{E(_caption(rows, before_s))}</p></div></section>')


def vision_md(rows, title: str = "Visão antes dos objetivos", before_s: int | None = None) -> list[str]:
    """Linhas de Markdown (título, tabela, legenda). Lista vazia se não houver linhas."""
    rows = _rows_of(rows)
    if not rows:
        return []
    cols = _cols(rows)
    md = lambda x: str(x).replace("|", "\\|")
    out = [f"## {title}", "", "| " + " | ".join(lbl for _, lbl, _ in cols) + " |",
           "|" + "|".join("---:" if num else "---" for _, _, num in cols) + "|"]
    out += ["| " + " | ".join(md(_cell(r, k)) for k, _, _ in cols) + " |" for r in rows]
    return out + ["", _caption(rows, before_s), ""]


# ----------------------------------------------------------------------------- CLI
class Out:
    def __init__(self, a):
        self.json, self.header, self._printed = a.json, not a.no_header, False

    def row(self, d: dict) -> None:
        if self.json:
            print(json.dumps(d, ensure_ascii=False))
            return
        if self.header and not self._printed:
            print("\t".join(d.keys()))
        self._printed = True
        print("\t".join("" if v is None else str(v) for v in d.values()))


def _focus(s: dict, sel: str) -> dict | None:
    return resolve_player(s["participants"], sel)


def cmd_game(a, out: Out) -> int:
    s = get_summary(int(str(a.gid).split("_")[-1]))
    p = _focus(s, a.player)
    if not p:
        print(f'Jogador "{a.player}" não encontrado na partida {s["gameId"]}. Use: lol players {s["gameId"]}', file=sys.stderr)
        return 1
    if not has_ward_events(s):
        print(NO_WARDS_MSG, file=sys.stderr)
        return 1
    if a.view == "objectives":
        res = vision_around_objectives(s, p["team"], a.before * 1000, a.margin)
        for r in res["rows"]:
            if a.json:
                out.row(r)
            else:
                out.row({k: _cell(r, k) if k in ("took", "edge") else r[k] for k in
                         ("time", "objective", "took", "ourPlaced", "ourControl", "theirPlaced", "theirControl",
                          "weCleared", "weClearedControl", "theyCleared", "theyClearedControl", "ourNet", "theirNet",
                          "edge", "vision")})
        g = res["game"]
        print(f'{g["objectives"]} objetivos ({p["champion"]}, time {p["team"]}): pegamos {g["nos"]}, eles {g["eles"]}; '
              f'na janela de {a.before}s: nossas {g["ourPlaced"]} col./{g["weCleared"]} destr., '
              f'deles {g["theirPlaced"]} col./{g["theyCleared"]} destr.; vantagem média {g["meanEdge"]}', file=sys.stderr)
    elif a.view == "bands":
        vs = vision_summary(s, p["team"])
        for b in vs["bands"]:
            out.row({"from": b["from"], "to": b["to"],
                     **{f"our{k[0].upper() + k[1:]}": v for k, v in b["us"].items()},
                     **{f"their{k[0].upper() + k[1:]}": v for k, v in b["them"].items()}})
    else:
        for r in vision_summary(s, p["team"])["players"]:
            out.row(r)
    return 0


def cmd_all(a, out: Out) -> int:
    rows, games, skipped = [], 0, 0
    for gid in cached_ids():
        s = get_summary(gid)
        if s["mapId"] != 11 or (s["duration"] or 0) < 300:
            continue
        if a.ranked and s["queueId"] not in RANKED:
            continue
        if a.queue is not None and s["queueId"] != a.queue:
            continue
        p = _focus(s, a.player)
        if not p:
            continue
        res = vision_around_objectives(s, p["team"], a.before * 1000, a.margin)
        if res is None:
            skipped += 1
            continue
        games += 1
        rows += res["rows"]
    if not rows:
        print("Nenhuma partida em cache com eventos de ward (fonte Match-V5) bate com os filtros.", file=sys.stderr)
        return 1
    for r in aggregate(rows):
        out.row(r)
    print(f"{games} partidas, {len(rows)} objetivos épicos (vastilarvas agrupadas); janela de {a.before}s; "
          f"{skipped} partida(s) sem eventos de ward (fonte LCU) ignorada(s). "
          f"Amostra pequena = menos de {SMALL_SAMPLE} casos.", file=sys.stderr)
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gid", nargs="?", help="gameId (ou use --all)")
    ap.add_argument("--all", action="store_true", help="agrega todas as partidas em cache")
    ap.add_argument("-p", "--player", default="me")
    ap.add_argument("--before", type=int, default=90, help="janela antes do abate, em segundos (padrão 90)")
    ap.add_argument("--margin", type=int, default=1, help="vantagem mínima para contar como mais/menos visão (padrão 1)")
    ap.add_argument("--view", choices=["objectives", "bands", "players"], default="objectives")
    ap.add_argument("--ranked", action="store_true", help="só Solo/Duo e Flex (com --all)")
    ap.add_argument("--queue", type=int, help="fila (com --all)")
    ap.add_argument("--json", action="store_true", help="JSON Lines em vez de TSV")
    ap.add_argument("--no-header", action="store_true")
    a = ap.parse_args()
    if not a.all and not a.gid:
        ap.error("informe um gameId ou --all")
    out = Out(a)
    try:
        code = cmd_all(a, out) if a.all else cmd_game(a, out)
    except BrokenPipeError:  # ex.: | head
        sys.stderr.close()
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
