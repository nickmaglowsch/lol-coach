"""Teste rápido de tudo o que dá para testar sem rede: `lol selftest [--fixture] [--keep]`.

1. Compila todos os módulos e procura sintaxe que só funciona no Python 3.12+ (o projeto promete 3.10+).
2. Zonas e referências do mapa em pontos conhecidos.
3. Com partidas em cache: roda as consultas do `lol` e renderiza um relatório de uma partida e um de
   várias partidas numa pasta temporária, conferindo seções, dados embutidos e (se houver Node) o JavaScript.
Sai com código 1 se algo falhar. Não baixa nada e não toca em reports/.

--fixture: em vez do cache real, gera partidas sintéticas (tools/fixture.py) numa pasta temporária, aponta
LOL_DATA_DIR para ela (este processo e os subprocessos) e roda as mesmas verificações, mais checagens dos
números que o roteiro garante e o mapa vetorial (sem minimapa nem retratos). É o modo do CI (sem dados pessoais).
--keep: não apaga a pasta temporária da fixture (para depurar).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((bool(ok), what))
    print(("  ok      " if ok else "  FALHOU  ") + what)
    return bool(ok)


# ----------------------------------------------------------------------------- 1. código
def pep701_issues(path: Path) -> list[str]:
    """f-strings que só compilam no 3.12+: aspas iguais às da f-string externa ou barra invertida
    dentro de uma expressão {…}."""
    issues = []
    stack: list[dict] = []  # f-strings abertas: aspas e profundidade de chaves
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline))
    except (tokenize.TokenError, SyntaxError) as e:
        return [f"{path.name}: não tokeniza ({e})"]
    for tok in toks:
        typ = tok.type
        if typ == getattr(tokenize, "FSTRING_START", None):
            q = tok.string.lstrip("rRfFbBuU")
            if stack and stack[-1]["depth"] > 0 and (q[0] == stack[-1]["q"][0] and len(stack[-1]["q"]) == 1):
                issues.append(f"{path.name}:{tok.start[0]} f-string com as mesmas aspas da f-string externa")
            stack.append({"q": q, "depth": 0})
            continue
        if typ == getattr(tokenize, "FSTRING_END", None):
            stack.pop()
            continue
        if not stack:
            continue
        top = stack[-1]
        if typ == tokenize.OP and tok.string == "{":
            top["depth"] += 1
        elif typ == tokenize.OP and tok.string == "}":
            top["depth"] = max(0, top["depth"] - 1)
        elif top["depth"] > 0 and typ == tokenize.STRING:
            body = tok.string.lstrip("rRbBuU")
            if len(top["q"]) == 1 and body[0] == top["q"][0]:
                issues.append(f"{path.name}:{tok.start[0]} string com as mesmas aspas da f-string externa")
            if "\\" in tok.string:
                issues.append(f"{path.name}:{tok.start[0]} barra invertida dentro de expressão de f-string")
    return issues


def test_code() -> None:
    print("1. Código")
    for py in sorted(TOOLS.glob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True, cfile=str(Path(tempfile.gettempdir()) / (py.stem + ".pyc")))
            ok = True
        except py_compile.PyCompileError as e:
            ok = False
            print("     ", e.msg.strip().splitlines()[-1])
        check(ok, f"compila {py.name}")
    issues = [i for py in sorted(TOOLS.glob("*.py")) for i in pep701_issues(py)]
    for i in issues:
        print("     ", i)
    check(not issues, "sem sintaxe exclusiva do Python 3.12+ (README promete 3.10+)")


# ----------------------------------------------------------------------------- 2. mapa
def test_map() -> None:
    print("2. Mapa")
    sys.path.insert(0, str(TOOLS))
    from landmarks import nearest
    from zones import MIRROR_X, MIRROR_Y, zone_abs
    camps = [(3821, 8101), (2288, 8448), (3780, 6443), (6943, 5422), (7765, 4020), (8394, 2641)]
    check(all(zone_abs(x, y) == "jungle_blue" and zone_abs(MIRROR_X - x, MIRROR_Y - y) == "jungle_red" for x, y in camps),
          "campos da jungle na jungle certa (azul e espelho vermelho)")
    check(all(zone_abs(x, y) == "river" for x, y in [(5007, 10471), (9866, 4414), (4400, 9600), (10500, 5100)]),
          "fossos e scuttles no rio")
    check(zone_abs(400, 400) == "base_blue" and zone_abs(14400, 14450) == "base_red", "fontes nas bases")
    check(nearest(13882, 4112)[0]["name"] == "torre externa bot vermelha", "referência mais próxima de um ponto conhecido")
    from landmarks import LANDMARKS
    from pregame import CAMPS
    bad = []
    for (sd, team), txt in CAMPS.items():
        names = [c.strip() for c in txt.replace(" ou ", ", ").split(",")]
        for n, x, y, t, kind, _ in LANDMARKS:
            if kind == "campo" and t == team and n.rsplit(" ", 1)[0] in names:
                got = "top" if y - x > 1200 else "bot" if y - x < -1200 else "centro"
                if got != sd:
                    bad.append(f"{n} ({sd})")
    check(not bad, "pré-jogo: campos sugeridos ficam no lado recomendado" + (f" (errados: {bad})" if bad else ""))


# ----------------------------------------------------------------------------- 3. dados
def run_lol(*args: str) -> subprocess.CompletedProcess:
    script = {"games": "query.py", "lanes": "query.py", "paths": "query.py", "players": "query.py", "frames": "query.py",
              "events": "query.py", "at": "query.py", "series": "query.py", "find": "query.py", "vision": "vision.py",
              "calibrate": "calibrate.py", "winprob": "winprob.py", "dashboard": "dashboard.py",
              "pregame": "pregame.py"}.get(args[0], "query.py")
    if script != "query.py":  # os outros scripts não recebem o nome do comando (como no lançador `lol`)
        args = args[1:]
    return subprocess.run([sys.executable, str(TOOLS / script), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def check_html(path: Path, ids: list[str], label: str) -> None:
    page = path.read_text(encoding="utf-8")
    missing = [i for i in ids if f'id="{i}"' not in page]
    check(not missing, f"{label}: seções presentes" + (f" (faltam {missing})" if missing else ""))
    body = page.split('<div class="wrap">', 1)[1].split("<script", 1)[0]
    visible = re.sub(r"<[^>]+>", " ", body)
    bad = [w for w in ("None", "undefined", "NaN", "{'", "nan") if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", visible)]
    check(not bad, f"{label}: sem 'None/undefined/NaN' no texto" + (f" (achou {bad})" if bad else ""))
    m = re.search(r'id="report-data">(.*?)</script>', page, re.S)
    try:
        json.loads(m.group(1).replace("<\\/", "</"))
        ok = True
    except Exception:
        ok = False
    check(ok, f"{label}: dados embutidos são JSON válido")
    node = shutil.which("node")
    if node:
        code = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
        js = Path(tempfile.gettempdir()) / "lolcoach_selftest.js"
        js.write_text(code, encoding="utf-8")
        r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        check(r.returncode == 0, f"{label}: JavaScript sem erro de sintaxe (node --check)")
    else:
        print("  (pulado) node não encontrado: JavaScript não verificado")


def test_data(fixture: dict | None = None) -> None:
    print("3. Consultas e relatórios (" + ("com a partida sintética" if fixture else "com o cache") + ")")
    sys.path.insert(0, str(TOOLS))
    from summarize import cached_ids, get_summary
    if fixture:
        # como no pipeline (`lol fetch` -> `lol summarize`): resumos gerados e gravados antes das consultas
        r = subprocess.run([sys.executable, str(TOOLS / "summarize.py"), "--all"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        check(r.returncode == 0, "lol summarize --all" + (f" ({r.stderr.strip().splitlines()[-1]})" if r.returncode else ""))
    ids = [g for g in sorted(cached_ids(), reverse=True)]
    games = [get_summary(g) for g in ids]
    sr = [s for s in games if s["mapId"] == 11 and (s["duration"] or 0) > 600]
    if not sr:
        print("  (pulado) nenhuma partida de Summoner's Rift em cache: rode `lol fetch latest` antes")
        return
    s = max(sr, key=lambda x: x["gameCreation"] or 0)
    gid = str(s["gameId"])
    for args in (["players", gid], ["frames", gid, "--to", "3"], ["events", gid, "--to", "10:00"], ["at", gid, "10:00"],
                 ["series", gid, "team"], ["games", "--n", "3"], ["lanes", "--n", "3"], ["paths", "--n", "3"], ["find"],
                 ["fights", gid], ["swings", gid], ["timers"], ["decisions", gid]):
        r = run_lol(*args)
        check(r.returncode == 0 and r.stdout.strip() != "", f"lol {' '.join(args[:1] + ['…'] if len(args) > 1 else args)}"
              + (f" ({r.stderr.strip().splitlines()[-1]})" if r.returncode else ""))
    # ferramentas de várias partidas: visão, calibração, chance de vitória (só se houver modelo treinado)
    from common import DATA
    extra = [["vision", "--all"], ["calibrate", "signals"], ["calibrate", "games"], ["calibrate", "score"]]
    if s["source"] != "lcu":
        extra.append(["vision", gid])
    if (DATA / "winprob" / "model.json").exists():
        extra += [["winprob", "curve", gid], ["winprob", "state", gid, "10:00"]]
    else:
        print("  (pulado) sem modelo de chance de vitória (data/winprob/model.json): `lol winprob` não testado")
    for args in extra:
        r = run_lol(*args)
        check(r.returncode == 0 and r.stdout.strip() != "", f"lol {' '.join(args[:2])}"
              + (f" ({(r.stderr.strip().splitlines() or ['sem saída'])[-1]})" if r.returncode or not r.stdout.strip() else ""))
    from decisions import infer_timers, review
    from winprob import prob_fn
    me = next((p for p in s["participants"] if p.get("role") == "JUNGLE"), s["participants"][0])
    decs = review(s, me, infer_timers(games))
    has_model = prob_fn(s, me["team"]) is not None
    check(bool(decs) and all((d["result"]["chance"] is not None) == has_model for d in decs),
          "revisão de decisões: chance de vitória " + ("presente (há modelo)" if has_model else "ausente sem modelo, sem erro"))
    if has_model:
        from winprob import state_at, win_prob
        st = state_at(s, 10)
        p1, p2 = win_prob(st, 100), win_prob(st, 200)
        check(0 < p1 < 1 and abs(p1 + p2 - 1) < 1e-9, "chance de vitória: entre 0 e 1 e os dois times somam 100%")
    if (DATA / "winprob" / "jungle_patterns.json").exists():
        r = run_lol("winprob", "junglers", "--min-games", "5")
        check(r.returncode == 0 and r.stdout.strip() != "", "lol junglers")
    # pré-jogo com os 10 campeões desta partida (sem cliente; roles dos inimigos inferidas)
    mine = next((p for p in s["participants"] if p.get("role") == "JUNGLE"), s["participants"][0])
    ally = ",".join(p["champion"] for p in s["participants"] if p["team"] == mine["team"])
    enemy = ",".join(p["champion"] for p in s["participants"] if p["team"] != mine["team"])
    r = run_lol("pregame", "--ally", ally, "--enemy", enemy, "--side", "blue" if mine["team"] == 100 else "red",
                "--me", mine["champion"])
    check(r.returncode == 0 and "O que esperar" in r.stdout and "Inimigo:" in r.stdout,
          "lol pregame (campeões de um print)" + (f" ({(r.stderr.strip().splitlines() or ['?'])[-1]})" if r.returncode else ""))
    r = run_lol("pregame", "--json", "--ally", ally, "--enemy", enemy)
    try:
        pj = json.loads(r.stdout)
        ok = len(pj["ally"]) == 5 and len(pj["enemy"]) == 5 and set(pj["enemy"]) == {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
    except Exception:
        ok = False
    check(ok, "lol pregame --json: 5 roles atribuídas em cada time")
    hp = Path(tempfile.mkdtemp(prefix="lolcoach-pregame-")) / "pregame.html"
    r = run_lol("pregame", "--ally", ally, "--enemy", enemy, "--side", "blue", "--html", str(hp))
    page = hp.read_text(encoding="utf-8") if hp.exists() else ""
    visible = re.sub(r"<[^>]+>", " ", page.split('<div class="wrap">', 1)[-1])
    bad = [w for w in ("None", "undefined", "NaN", "{{") if w in visible or (w == "{{" and w in page)]
    check(r.returncode == 0 and "<title>Pré-jogo" in page and "<svg" in page and not bad,
          "lol pregame --html: página do celular" + (f" (achou {bad})" if bad else ""))
    shutil.rmtree(hp.parent, ignore_errors=True)

    import render as render_mod
    from render import render
    if fixture:  # igual ao CI: sem o minimapa (arquivo local fora do git) nem retratos baixados
        render_mod.MINIMAP = Path(fixture["dataDir"]) / "sem-minimapa.png"
        render_mod.ICONS = Path(fixture["dataDir"]) / "static" / "champion-icons"
    tmp = Path(tempfile.mkdtemp(prefix="lolcoach-selftest-"))
    kill = next((k for k in s["events"]["kills"] if k["ts"] > 120000), None)
    top = next((p for p in s["participants"] if p.get("role") == "TOP"), None)
    spec = {
        "gameId": int(gid), "focus": "me" if any(p.get("puuid") for p in s["participants"]) else 1, "tema": "selftest",
        "title": "Selftest", "tldr": [{"title": "Card", "text": "Texto com **negrito** às 10:00."}, "Texto simples."],
        "verdicts": [{"q": "Pergunta de teste?", "verdict": "INCONCLUSIVO", "points": ["Ponto com minuto 10:00."]}],
        "gold": {"bands": [["10:00", "12:00", "faixa"]], "notes": [["10:00", "team", "nota"]]},
        "strip": {"from": 1, "to": 10}, "pathMap": {"from": 1, "to": 10},
        "recs": [{"title": "Rec", "text": "Meta mensurável até 15:00."}],
        "sections": [{"title": "Seção", "points": ["Item às 05:00."]}],
    }
    from common import resolve_player
    if resolve_player(s["participants"], "me") is None:
        spec["focus"] = 1
    if top:
        spec["laneChart"] = {"role": "TOP", "from": 2, "to": 10}
    if kill:
        t0 = kill["ts"] // 1000
        spec["fights"] = [{"title": "Luta", "from": f"{t0 // 60}:{t0 % 60:02d}", "to": f"{(t0 + 30) // 60}:{(t0 + 30) % 60:02d}",
                           "positionsAt": f"{t0 // 60}:00"}]
    p = tmp / "selftest-uma.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    try:
        outs = render(p, None)
        ok = len(outs) == 2
    except SystemExit as e:
        ok, outs = False, []
        print("     ", e)
    check(ok, f"render de uma partida ({gid})")
    if ok:
        ids_expected = ["goldChart", "replayMap", "rpTrack", "rpSlider", "objChart", "strip", "mapPath", "decisoes"]
        ids_expected += ["laneChart"] if top else []
        ids_expected += ["mapFight0"] if kill else []
        check_html(p.with_suffix(".html"), ids_expected, "uma partida")
        if s["source"] != "lcu":
            check("Visão antes dos objetivos" in p.with_suffix(".html").read_text(encoding="utf-8"),
                  "uma partida: seção de visão antes dos objetivos")
        md = p.with_suffix(".md").read_text(encoding="utf-8")
        check(all(h in md for h in ("## Resumo em 5 linhas", "## Linha do tempo", "## Recomendações", "## Limitações")),
              "uma partida: Markdown com as seções obrigatórias")
        if fixture:
            page = p.with_suffix(".html").read_text(encoding="utf-8")
            data = json.loads(re.search(r'id="report-data">(.*?)</script>', page, re.S).group(1).replace("<\\/", "</"))
            check(data.get("mapImg") is None and data.get("replay") and data.get("structures"),
                  "uma partida: sem minimapa, replay e estruturas no mapa vetorial")
            check(len(data.get("fights") or []) == 1 and data.get("decisions"), "uma partida: luta e decisões nos dados")

    # esqueleto do `lol new`: render recusa TODO; com --draft gera
    r = subprocess.run([sys.executable, str(TOOLS / "scaffold.py"), "spec", gid], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    ok = r.returncode == 0 and '"TODO' in r.stdout
    check(ok, "lol spec gera esqueleto com TODO")
    if ok:
        sk = tmp / "selftest-esqueleto.json"
        sk.write_text(r.stdout, encoding="utf-8")
        try:
            render(sk, None)
            refused = False
        except SystemExit:
            refused = True
        check(refused, "render recusa spec com TODO")
        try:
            render(sk, None, draft=True)
            ok2 = True
        except SystemExit as e:
            ok2 = False
            print("     ", e)
        check(ok2, "render --draft aceita o esqueleto")
        if ok2:
            check('id="decisoes"' in sk.with_suffix(".html").read_text(encoding="utf-8"), "esqueleto já traz a revisão de decisões")

    multi = {"mode": "multi", "focus": spec["focus"] if spec["focus"] == "me" else "me", "tema": "selftest", "title": "Selftest",
             "n": 10, "compare": [{"label": "Vitórias", "result": "win"}, {"label": "Derrotas", "result": "loss"}],
             "tldr": ["Resumo."], "verdicts": [{"q": "Teste?", "verdict": "MISTO", "points": ["3 de 10 partidas."]}]}
    p2 = tmp / "selftest-varias.json"
    p2.write_text(json.dumps(multi, ensure_ascii=False), encoding="utf-8")
    try:
        render(p2, None)
        ok = True
    except SystemExit as e:
        ok = False
        print("     ", e)
    check(ok, "render de várias partidas")
    if ok:
        check_html(p2.with_suffix(".html"), ["multiGold", "deathBars"], "várias partidas")
        check('class="tbl cmp"' in p2.with_suffix(".html").read_text(encoding="utf-8"), "várias partidas: tabela de comparação")

    # painel de evolução
    from types import SimpleNamespace
    import dashboard
    dp = tmp / "painel.html"
    try:
        dashboard.build(SimpleNamespace(ranked=False, queue=None, role=None, since=None, until=None, recent=None, window=None,
                                        alias="teste", out=str(dp)))
        ok = dp.exists()
    except SystemExit as e:
        ok = False
        print("     ", e)
    check(ok, "lol dashboard gera o painel")
    if ok:
        page = dp.read_text(encoding="utf-8")
        m = re.search(r'id="dash-data"[^>]*>(.*?)</script>', page, re.S)
        try:
            json.loads(m.group(1).replace("<\\/", "</"))
            ok = True
        except Exception:
            ok = False
        check(ok, "painel: dados embutidos são JSON válido")
        node = shutil.which("node")
        if node:
            code = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
            js = Path(tempfile.gettempdir()) / "lolcoach_selftest_dash.js"
            js.write_text(code, encoding="utf-8")
            r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            check(r.returncode == 0, "painel: JavaScript sem erro de sintaxe (node --check)")
    shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- 4. fixture
def test_fixture(fx: dict) -> None:
    """Números que o roteiro da fixture garante (tools/fixture.py): se mudarem, algo no resumo mudou."""
    print("4. Partida sintética: números do roteiro")
    from common import DATA, SUMMARIES, resolve_player
    from summarize import get_summary
    check(DATA == Path(fx["dataDir"]).resolve() and (SUMMARIES / f'{fx["main"]}.json').exists(),
          "LOL_DATA_DIR: resumos gravados na pasta da fixture")
    check(not (TOOLS.parent / "data" / "summaries" / f'{fx["main"]}.json').exists(), "nada gravado no data/ real")
    s = get_summary(fx["main"])
    ev, P = s["events"], s["participants"]
    me = resolve_player(P, "me")
    check(me is not None and me["riotId"] == fx["me"] and me["role"] == "JUNGLE" and me["stats"]["win"],
          "me.json resolve o jogador em foco (jungle, vitória)")
    check(resolve_player(P, "chogath") is not None and resolve_player(P, "Kha'Zix") is not None,
          "campeões com apóstrofo (Cho'Gath, Kha'Zix) pelo nome normalizado")
    check(s["source"] == "riot-match-v5" and all(p["roleSource"] == "match-v5 teamPosition" for p in P),
          "fonte Match-V5 e roles do teamPosition")
    check(len(s["perMinute"]) == 30 and s["perMinute"][10]["players"][str(me["pid"])].get("hpPct") is not None,
          "30 frames, com vida/mana (championStats)")
    kinds: dict[str, int] = {}
    for o in ev["objectives"]:
        kinds[o["type"]] = kinds.get(o["type"], 0) + 1
    want = {"DRAGON": 4, "HORDE": 3, "RIFTHERALD": 1, "BARON_NASHOR": 1, "TOWER": 9, "INHIBITOR": 1, "TURRET_PLATE": 5}
    check(len(ev["kills"]) == 15 and kinds == want, f"15 abates e os objetivos do roteiro (achou {len(ev['kills'])} e {kinds})")
    check(bool(ev["wards"]["events"]) and any(b["type"] == "ITEM_UNDO" for b in ev["purchases"])
          and not any(b["estimated"] for b in ev["purchases"]), "wards e compras por evento (com ITEM_UNDO)")
    check(any(r["pid"] == me["pid"] and r["kind"].startswith("recall") for r in ev["recalls"])
          and any(r["kind"] == "volta à base após morte" for r in ev["recalls"]), "recalls inferidos pelas compras")
    fb = ev["kills"][0]
    check(fb["minute"] == 3 and fb["killer"] == me["pid"] and fb["zoneForVictim"] == "top",
          "primeiro abate: gank do jungle no top no minuto 3")
    r = run_lol("fights", str(fx["main"]), "--no-header")
    check(r.returncode == 0 and len(r.stdout.strip().splitlines()) == 3, "lol fights acha as 3 lutas do roteiro")
    r = run_lol("games", "--no-header")
    res = sorted(ln.split("\t")[5] for ln in r.stdout.strip().splitlines()) if r.returncode == 0 else []
    check(res == ["False", "True", "True"], "lol games: 3 partidas (2 vitórias e a derrota espelhada)")
    r = run_lol("find", "--enemy", "khazix", "--loss", "--no-header")
    check(r.returncode == 0 and r.stdout.split("\t")[0].strip() == "9100000002", "lol find --enemy --loss acha a partida espelhada")
    r = run_lol("at", str(fx["main"]), "03:20", "--ref", "1700,12700", "--json")
    rows = [json.loads(ln) for ln in r.stdout.strip().splitlines()] if r.returncode == 0 else []
    check(len(rows) == 10, "lol at devolve os 10 jogadores")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", action="store_true", help="usa partidas sintéticas numa pasta temporária (modo do CI)")
    ap.add_argument("--keep", action="store_true", help="com --fixture: não apaga a pasta temporária")
    a = ap.parse_args()
    fx, fx_dir = None, None
    if a.fixture:
        sys.path.insert(0, str(TOOLS))
        import fixture
        fx_dir = Path(tempfile.mkdtemp(prefix="lolcoach-fixture-"))
        fx = fixture.generate(fx_dir / "data")
        os.environ["LOL_DATA_DIR"] = fx["dataDir"]   # antes de importar common: vale aqui e nos subprocessos
        print(f"(fixture: {len(fx['games'])} partidas sintéticas em {fx['dataDir']})")
    test_code()
    test_map()
    try:
        test_data(fx)
        if fx:
            test_fixture(fx)
    except Exception as e:  # um erro inesperado também é falha do teste
        check(False, f"erro inesperado: {e.__class__.__name__}: {e}")
    if fx_dir and not a.keep:
        shutil.rmtree(fx_dir, ignore_errors=True)
    bad = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} verificações ok" + (f"; falharam: {len(bad)}" if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
