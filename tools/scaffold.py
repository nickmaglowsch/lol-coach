"""Esqueleto de spec: começa toda análise com os dados já escolhidos e o julgamento marcado como TODO.

  lol spec <gameId> [--tema review] [-p me]            imprime o spec de uma partida no stdout
  lol spec --multi [--tema T] [--ranked] [--role R] [--since D] [--until D] [--queue Q]
  lol new <gameId|latest|latest-flex|latest-solo> [--tema review]   baixa, resume e grava reports/<data>-<id>-<tema>.json
  lol new --multi --tema <tema> [filtros…]             grava reports/<data>-multi-<tema>.json

O esqueleto já traz: lutas maiores (`lol fights`), viradas de ouro (`lol swings`) como faixas e anotações do
gráfico, faixa/caminho do jungle, a lane mais atrás aos 15 min e as decisões com mais sinais (`lol decisions`)
com "notes" em TODO. Os textos de julgamento (título, resumo, vereditos, recomendações, comentários) ficam em
"TODO: …" e o `lol render` recusa publicar enquanto houver algum (use `--draft` para pré-visualizar).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from common import DATA, REPORTS, load_json, resolve_player
from query import fights_of, swings_of
from summarize import ROLE_PT, get_summary

TOOLS = Path(__file__).resolve().parent
TODO = "TODO"


def me_alias() -> str | None:
    p = DATA / "me.json"
    if not p.exists():
        return None
    me = load_json(p)
    return me.get("alias") or me.get("gameName")


def single_spec(gid: int, tema: str, who: str = "me") -> dict:
    s = get_summary(gid)
    focus = resolve_player(s["participants"], who) or s["participants"][0]
    team = focus["team"]
    win = focus["stats"]["win"]
    spec: dict = {
        "gameId": s["gameId"], "focus": who if resolve_player(s["participants"], who) else focus["pid"],
        "alias": me_alias() if who == "me" else None, "tema": tema,
        "pageTitle": f'{focus["champion"]} · {"vitória" if win else "derrota"} {s["date"][8:10]}/{s["date"][5:7]}',
        "title": f"{TODO}: título com a conclusão principal",
        "tldr": [{"title": f"{TODO}", "text": f"{TODO}: até 5 cards, cada um com minuto e dado"} for _ in range(3)],
        "verdicts": [{"q": f"{TODO}: pergunta ou alegação do usuário", "verdict": "INCONCLUSIVO",
                      "points": [f"{TODO}: ponto com minuto e dado"]}],
        "recs": [{"title": f"{TODO}: recomendação acionável", "text": f"{TODO}: meta mensurável"}],
    }
    if spec["alias"] is None:
        spec.pop("alias")
    if s["mapId"] != 11:
        spec["replay"] = False
        return spec
    # gráfico de ouro: as maiores viradas viram faixas e anotações
    sw = swings_of(s, team, 3, 3)
    spec["gold"] = {"bands": [[w["from"], w["to"], f'{w["delta"]:+,d}'.replace(",", ".").replace("-", "−")] for w in sw if abs(w["delta"]) >= 2500],
                    "notes": [[w["to"], "team", f'{w["from"]}–{w["to"]} {w["delta"]:+,d}'.replace(",", ".")] for w in sw]}
    # jungle: faixa de zonas e caminho no começo
    if focus.get("role") == "JUNGLE":
        spec["strip"] = {"from": 1, "to": 15}
        spec["pathMap"] = {"from": 1, "to": 15}
    # lane do time mais atrás aos 15
    at15 = next((r for r in s["perMinute"] if r["minute"] == 15), None)
    if at15:
        lanes = [(at15["players"][str(p["pid"])].get("goldDiff"), p) for p in s["participants"]
                 if p["team"] == team and p.get("role") in ("TOP", "MIDDLE", "BOTTOM") and p is not focus]
        lanes = [x for x in lanes if x[0] is not None]
        if lanes:
            gd, p = min(lanes, key=lambda x: x[0])
            if gd < -500:
                spec["laneChart"] = {"role": p["role"], "from": 2, "to": 15}
    # lutas maiores (por número de abates, desempate pelo saldo)
    fs = sorted(fights_of(s, team), key=lambda f: (f["kills"], abs(f["ours"] - f["theirs"])), reverse=True)[:2]
    if fs:
        spec["fights"] = [{"title": f'Luta {f["from"]}–{f["to"]}', "eyebrow": f'{f["ours"]}×{f["theirs"]}', "from": f["from"], "to": f["to"],
                           "positionsAt": f'{f["fromMs"] // 60000}:00', "caption": f"{TODO}: o que essa luta mostra"}
                          for f in sorted(fs, key=lambda f: f["fromMs"])]
    # decisões com mais sinais / impacto: comentário em TODO
    from decisions import infer_timers, review
    from summarize import cached_ids
    decs = review(s, focus, infer_timers([get_summary(g) for g in cached_ids()]))
    cand = sorted((d for d in decs if d["signals"] or abs(d["score"]) >= 1000),
                  key=lambda d: abs(d["score"]) + 300 * len(d["signals"]), reverse=True)
    picked, used = [], []
    for d in cand:
        if any(abs(d["ts"] - u) < 45000 for u in used):
            continue
        picked.append(d); used.append(d["ts"])
        if len(picked) >= 8:
            break
    picked.sort(key=lambda d: d["ts"])
    spec["decisions"] = {"pick": [d["time"] for d in picked],
                         "notes": {d["time"]: f'{TODO}: comentário ({d["what"]}; sinais: {", ".join(d["signals"]) or "nenhum"}; '
                                              f'janela {d["window"]} → {d["verdict"]})' for d in picked}}
    return spec


def multi_spec(tema: str, args) -> dict:
    spec = {"mode": "multi", "focus": "me", "alias": me_alias(), "tema": tema,
            "pageTitle": f"{TODO}: nome curto", "title": f"{TODO}: pergunta ou conclusão",
            "tldr": [{"title": TODO, "text": f"{TODO}: card com contagem"} for _ in range(3)],
            "verdicts": [{"q": f"{TODO}: pergunta", "verdict": "INCONCLUSIVO", "points": [f"{TODO}: ponto com contagem"]}],
            "recs": [{"title": f"{TODO}: recomendação", "text": f"{TODO}: meta mensurável"}],
            "compare": [{"label": "Vitórias", "result": "win"}, {"label": "Derrotas", "result": "loss"}]}
    for k in ("ranked", "role", "since", "until", "queue"):
        v = getattr(args, k, None)
        if v:
            spec[k] = v
    if not spec["alias"]:
        spec.pop("alias")
    return spec


def resolve_target(target: str) -> int:
    if target.split("_")[-1].isdigit():
        return int(target.split("_")[-1])
    r = subprocess.run([sys.executable, str(TOOLS / "fetch.py"), "fetch", target], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sys.stderr.write(r.stderr)
    ids = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().isdigit()]
    if r.returncode or not ids:
        raise SystemExit(f"Não consegui buscar '{target}'.")
    return int(ids[-1])


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["spec", "new"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--tema", default="review")
    ap.add_argument("-p", "--player", default="me")
    ap.add_argument("--multi", action="store_true")
    ap.add_argument("--ranked", action="store_true"); ap.add_argument("--role"); ap.add_argument("--queue", type=int)
    ap.add_argument("--since"); ap.add_argument("--until")
    ap.add_argument("--force", action="store_true", help="sobrescreve o spec se já existir")
    a = ap.parse_args()
    if a.multi:
        spec, name = multi_spec(a.tema, a), f"{dt.date.today():%Y-%m-%d}-multi-{a.tema}.json"
    else:
        if not a.target:
            ap.error("informe o gameId (ou latest/latest-flex/latest-solo com `new`)")
        gid = resolve_target(a.target) if a.cmd == "new" else int(a.target.split("_")[-1])
        spec, name = single_spec(gid, a.tema, a.player), f"{dt.date.today():%Y-%m-%d}-{gid}-{a.tema}.json"
    text = json.dumps(spec, ensure_ascii=False, indent=2) + "\n"
    if a.cmd == "spec":
        print(text, end="")
        return
    path = REPORTS / name
    if path.exists() and not a.force:
        print(f"[info] {path.name} já existe; não sobrescrevi (use --force).", file=sys.stderr)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        todos = text.count(TODO)
        print(f"[ok] esqueleto com {todos} TODO(s). Investigue com `lol decisions/fights/swings`, preencha e rode `lol render`.",
              file=sys.stderr)
    print(path)


if __name__ == "__main__":
    main()
