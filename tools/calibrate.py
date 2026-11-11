"""Calibra a revisão de decisões (tools/decisions.py) com as partidas em cache do próprio jogador.

Usa todas as partidas de Summoner's Rift em cache (mapId 11, mais de 10 min) em que o jogador (-p, padrão "me")
aparece. Saída TSV no stdout (cabeçalho; --no-header; --json = JSON Lines); mensagens no stderr.

  python tools/calibrate.py signals      cada tipo de sinal: decisões com × sem o sinal (Δouro do time na janela,
                                         taxa de "ruim", vitória) e se ele antecede resultado pior
  python tools/calibrate.py games        o veredito separa partidas? decisões "ruim" por partida, vitórias × derrotas
                                         (--each: uma linha por partida)
  python tools/calibrate.py epics        valor de um objetivo épico em ouro: (1) salto do ouro do time nos 2 min
                                         seguintes; (2) equivalência na chance de vitória (regressão logística)
  python tools/calibrate.py score        distribuição do placar da janela e cortes GOOD/BAD candidatos
  python tools/calibrate.py thresholds   limiares alternativos de cada sinal (vida, bolso, distância do jungle...)
  python tools/calibrate.py all [--write]  roda tudo, imprime os valores escolhidos; --write grava data/calibration.json
                                         (que tools/decisions.py lê por cima dos padrões)

Limitações: um único jogador (amostra pequena, mesmo elo/estilo); o veredito é pelo resultado da janela (viés de
resultado: em derrotas quase toda janela perde ouro); as janelas se sobrepõem e as decisões de uma mesma partida não
são independentes (os intervalos de confiança são otimistas); posições só a cada minuto.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date

from common import save_json, resolve_player
import decisions as D
from summarize import cached_ids, get_summary

EPIC_KEYS = ("DRAGON", "ELDER", "HORDE", "RIFTHERALD", "BARON_NASHOR", "ATAKHAN")
EPIC_LABEL = {"DRAGON": "dragão", "ELDER": "ancião", "HORDE": "vastilarva (cada)", "RIFTHERALD": "arauto",
              "BARON_NASHOR": "barão", "ATAKHAN": "atakhan"}
SMALL = 30          # abaixo disto (decisões com o sinal) a amostra é pequena
LOGIT_MINUTES = (10, 14, 18, 22, 26, 30)


def log(*a) -> None:
    print(*a, file=sys.stderr)


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
        print("\t".join("" if v is None else (",".join(map(str, v)) if isinstance(v, list) else str(v)) for v in d.values()))


# ----------------------------------------------------------------------------- dados
def load_games(who: str = "me", role: str | None = None) -> list[tuple[dict, dict]]:
    out = []
    for gid in sorted(cached_ids()):
        s = get_summary(gid)
        if s["mapId"] != 11 or (s["duration"] or 0) <= 600:
            continue
        p = resolve_player(s["participants"], who)
        if not p or (role and p.get("role") != role.upper()):
            continue
        out.append((s, p))
    return out


def all_timers() -> dict:
    return D.infer_timers([get_summary(g) for g in cached_ids()])


def collect(games: list[tuple[dict, dict]]) -> list[dict]:
    """Todas as decisões, com a partida e o resultado dela."""
    timers = all_timers()
    rows = []
    for s, p in games:
        won = s["winnerTeam"] == p["team"]
        for d in D.review(s, p, timers):
            d["gameId"], d["won"] = s["gameId"], won
            rows.append(d)
    log(f"{len(games)} partidas, {len(rows)} decisões")
    return rows


def score_of(d: dict, weights: dict, default: int) -> int:
    r = d["result"]
    return r["ouroTime"] + sum(weights.get(k, default) for k in r["epicosNossos"]) - sum(weights.get(k, default) for k in r["epicosDeles"])


def verdict(score: float, good: float, bad: float) -> str:
    return "boa" if score >= good else "ruim" if score <= bad else "neutra"


def current_params() -> dict:
    return {"weights": dict(D.EPIC_WEIGHTS), "default": D.EPIC_WEIGHT, "good": D.GOOD, "bad": D.BAD}


# ----------------------------------------------------------------------------- estatística
def mean(xs):
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def diff_ci(a: list, b: list):
    """Diferença de médias (a − b) e meia-largura do IC 95% (Welch, aproximado)."""
    if len(a) < 2 or len(b) < 2:
        return (mean(a) - mean(b) if a and b else None), None
    se = math.sqrt(sd(a) ** 2 / len(a) + sd(b) ** 2 / len(b))
    return mean(a) - mean(b), 1.96 * se


def pct(n, d):
    return round(100 * n / d) if d else None


def rnd(x, k=0):
    return None if x is None else (round(x) if k == 0 else round(x, k))


def percentile(xs: list, q: float):
    xs = sorted(xs)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def auc(pos: list, neg: list):
    """P(valor de um item de `pos` > valor de um de `neg`) (empates = 0,5)."""
    if not pos or not neg:
        return None
    w = sum((1 if a > b else 0.5 if a == b else 0) for a in pos for b in neg)
    return w / (len(pos) * len(neg))


def solve(A: list, b: list) -> list:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def logit_fit(X: list, y: list, ridge: float = 1.0, iters: int = 40) -> list:
    """Regressão logística por Newton com penalidade L2 leve (não penaliza o intercepto, coluna 0)."""
    k = len(X[0])
    b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = max(-30.0, min(30.0, sum(bj * xj for bj, xj in zip(b, xi))))
            p = 1 / (1 + math.exp(-z))
            w = p * (1 - p)
            for j in range(k):
                g[j] += (yi - p) * xi[j]
                wx = w * xi[j]
                for l in range(j, k):
                    H[j][l] += wx * xi[l]
        for j in range(k):
            for l in range(j):
                H[j][l] = H[l][j]
        for j in range(1, k):
            g[j] -= ridge * b[j]
            H[j][j] += ridge
        step = solve(H, g)
        b = [bj + sj for bj, sj in zip(b, step)]
        if max(abs(x) for x in step) < 1e-7:
            break
    return b


# ----------------------------------------------------------------------------- (a) sinais
def eligible(cat: str, d: dict) -> bool:
    """A decisão podia ter o sinal (tipo certo e dado disponível)?"""
    if d["kind"] not in D.SIGNALS[cat][1]:
        return False
    st = d["state"]
    if cat == "vida baixa":
        return st.get("hp") is not None
    if cat == "ouro no bolso":
        return st.get("bolso") is not None and d["ts"] < D.LIMITS["bolsoAte"]
    if cat == "lane níveis":
        return d["lane"] is not None
    if cat == "lane vida":
        return d["lane"] is not None and d["lane"].get("dVida") is not None
    if cat == "jungle inimigo perto":
        return d["enemyJungle"] is not None
    if cat == "objetivo vivo do outro lado":
        return bool(d.get("toSide"))
    return True


def compare(rows: list, has, P: dict) -> dict:
    """Decisões com × sem um sinal (has(d) -> bool) entre as elegíveis em `rows`. Mede o placar da janela (Δouro do
    time + peso dos épicos), o Δouro sozinho, a taxa de "ruim" e a de épico perdido na janela.
      diffPorTipo  : diferença dentro de cada tipo de decisão (lane, recall...), pesada pelo nº com o sinal;
      diffNaPartida: diferença dentro da mesma partida (média entre partidas que têm os dois casos) — tira o efeito
                     de "partida já perdida", que pesa muito num histórico de um jogador só."""
    w = [d for d in rows if has(d)]
    wo = [d for d in rows if not has(d)]
    sc = lambda ds: [score_of(d, P["weights"], P["default"]) for d in ds]
    g = lambda ds: [d["result"]["ouroTime"] for d in ds]
    diff, ci = diff_ci(sc(w), sc(wo))
    gdiff, gci = diff_ci(g(w), g(wo))
    num = den = 0
    for k in {d["kind"] for d in w}:
        a, b = [x for x in w if x["kind"] == k], [x for x in wo if x["kind"] == k]
        if a and b:
            num += len(a) * (mean(sc(a)) - mean(sc(b)))
            den += len(a)
    adj = num / den if den else None
    per = defaultdict(lambda: ([], []))
    for d in rows:
        per[d["gameId"]][0 if has(d) else 1].append(score_of(d, P["weights"], P["default"]))
    pairs = [mean(a) - mean(b) for a, b in per.values() if a and b]
    is_bad = lambda d: verdict(score_of(d, P["weights"], P["default"]), P["good"], P["bad"]) == "ruim"
    rw, rwo = (sum(map(is_bad, w)) / len(w) if w else None), (sum(map(is_bad, wo)) / len(wo) if wo else None)
    rci = 1.96 * math.sqrt(rw * (1 - rw) / len(w) + rwo * (1 - rwo) / len(wo)) if w and wo else None
    lost = lambda ds: pct(sum(1 for d in ds if d["result"]["epicosDeles"]), len(ds))
    games_with = {d["gameId"]: d["won"] for d in w}
    games_without = {d["gameId"]: d["won"] for d in wo if d["gameId"] not in games_with}
    return {"n": len(w), "nSem": len(wo), "partidas": len(games_with),
            "scoreCom": rnd(mean(sc(w))), "scoreSem": rnd(mean(sc(wo))), "diff": rnd(diff), "ic95": rnd(ci),
            "diffPorTipo": rnd(adj), "diffNaPartida": rnd(mean(pairs)),
            "icNaPartida": rnd(1.96 * sd(pairs) / math.sqrt(len(pairs))) if len(pairs) > 2 else None,
            "partidasPareadas": len(pairs), "diffOuro": rnd(gdiff), "icOuro": rnd(gci),
            "ruimCom": rnd(100 * rw) if rw is not None else None, "ruimSem": rnd(100 * rwo) if rwo is not None else None,
            "icRuim": rnd(100 * rci) if rci is not None else None,
            "epicoPerdidoCom": lost(w), "epicoPerdidoSem": lost(wo),
            "vitoriaCom": pct(sum(games_with.values()), len(games_with)),
            "vitoriaSem": pct(sum(games_without.values()), len(games_without))}


def conclude(c: dict) -> str:
    """antecede pior: placar da janela pior com o sinal (IC95 abaixo de zero, ou taxa de "ruim" maior com IC95), e
    pior também dentro do mesmo tipo de decisão e dentro da mesma partida; "(forte)" se a diferença na mesma partida
    também tem IC95 abaixo de zero. tendência pior: as três diferenças negativas, sem significância."""
    if c["diff"] is None or c["ic95"] is None:
        return "amostra pequena"
    small = " (amostra pequena)" if c["n"] < SMALL else ""
    neg = (c["diffPorTipo"] or 0) < 0 and (c["diffNaPartida"] or 0) < 0
    sig = c["diff"] + c["ic95"] < 0 or (c["icRuim"] is not None and c["ruimCom"] - c["ruimSem"] > c["icRuim"])
    paired = c["icNaPartida"] is not None and c["diffNaPartida"] + c["icNaPartida"] < 0
    if sig and neg:
        return "antecede pior" + (" (forte)" if paired else "") + small
    if c["diff"] < 0 and neg:
        return "tendência pior" + small
    if c["diff"] - c["ic95"] > 0:
        return "antecede melhor" + small
    return "sem efeito claro" + small


def signal_table(rows: list, P: dict) -> list[dict]:
    out = []
    for cat in D.SIGNALS:
        el = [d for d in rows if eligible(cat, d)]
        c = compare(el, lambda d, cat=cat: cat in d["signalCats"], P)
        c = {"sinal": cat, "elegiveis": len(el), **c}
        c["conclusao"] = conclude(c)
        out.append(c)
    return out


# ----------------------------------------------------------------------------- (b) partidas
def game_table(rows: list, P: dict, until_ms: int | None = None) -> dict:
    per = defaultdict(lambda: {"n": 0, "ruim": 0, "boa": 0, "won": None})
    for d in rows:
        if until_ms is not None and d["ts"] >= until_ms:
            continue
        g = per[d["gameId"]]
        g["won"] = d["won"]
        g["n"] += 1
        v = verdict(score_of(d, P["weights"], P["default"]), P["good"], P["bad"])
        g[v] = g.get(v, 0) + 1
    return per


def games_summary(per: dict, label: str) -> list[dict]:
    out = []
    share = {gid: g["ruim"] / g["n"] for gid, g in per.items() if g["n"]}
    for name, won in (("vitórias", True), ("derrotas", False)):
        gs = [g for g in per.values() if g["won"] is won and g["n"]]
        out.append({"janela": label, "grupo": name, "partidas": len(gs),
                    "decisoesPorPartida": rnd(mean([g["n"] for g in gs]), 1),
                    "ruimPorPartida": rnd(mean([g["ruim"] for g in gs]), 1),
                    "pctRuim": rnd(100 * mean([g["ruim"] / g["n"] for g in gs]) if gs else None),
                    "pctBoa": rnd(100 * mean([g["boa"] / g["n"] for g in gs]) if gs else None)})
    a = auc([share[k] for k, g in per.items() if g["won"] is False and g["n"]],
            [share[k] for k, g in per.items() if g["won"] is True and g["n"]])
    for r in out:
        r["aucDerrota"] = rnd(a, 2)
    return out


# ----------------------------------------------------------------------------- (c) épicos
def epic_counts(s: dict, ts: int) -> dict:
    """Épicos de cada time até ts: {team: {key: n}}."""
    c = {100: defaultdict(int), 200: defaultdict(int)}
    for o in s["events"]["objectives"]:
        if o["ts"] <= ts and o["type"] in D.EPIC and o["killerTeam"] in (100, 200):
            c[o["killerTeam"]][D.epic_key(o)] += 1
    return c


def epic_swings(games: list[tuple[dict, dict]], after_ms: int = 120000) -> dict:
    """Variação do ouro do time que pegou o épico, do frame anterior ao abate até o frame de ts+2 min.
    Vastilarvas que caem juntas (mesmo time, < 90 s) contam como um acampamento."""
    out = defaultdict(list)
    for s, _ in games:
        end = s["perMinute"][-1]["ts"]
        last = {}
        for o in sorted(s["events"]["objectives"], key=lambda o: o["ts"]):
            if o["type"] not in D.EPIC or o["killerTeam"] not in (100, 200) or o["ts"] + after_ms > end:
                continue
            k = D.epic_key(o)
            if k == "HORDE":
                prev = last.get((k, o["killerTeam"]))
                last[(k, o["killerTeam"])] = o["ts"]
                if prev is not None and o["ts"] - prev < 90000:
                    continue
            a, b = D.frame_before(s, o["ts"]), D.frame_before(s, o["ts"] + after_ms)
            out[k].append(D.team_gold(s, b, o["killerTeam"]) - D.team_gold(s, a, o["killerTeam"]))
    return out


def logit_samples(games: list[tuple[dict, dict]], keys: tuple) -> tuple[list, list, list]:
    """Amostras (minuto m de cada partida): [1, ouro do azul − vermelho (mil), diferença de épicos por tipo...]."""
    X, y, gid = [], [], []
    for s, _ in games:
        pm = s["perMinute"]
        for m in LOGIT_MINUTES:
            if m >= len(pm) - 1:
                break
            row = pm[m]
            c = epic_counts(s, row["ts"])
            X.append([1.0, row["teamDiff100"]["gold"] / 1000] + [c[100][k] - c[200][k] for k in keys])
            y.append(1 if s["winnerTeam"] == 100 else 0)
            gid.append(s["gameId"])
    return X, y, gid


def gold_equiv(X, y, gid, n_keys: int, boot: int, seed: int = 7) -> list[tuple]:
    """Para cada tipo: (equivalência em ouro, IC 90% por bootstrap de partidas)."""
    b = logit_fit(X, y)
    eq = [1000 * b[2 + j] / b[1] if b[1] > 0 else None for j in range(n_keys)]
    by = defaultdict(list)
    for i, g in enumerate(gid):
        by[g].append(i)
    games = list(by)
    rng = random.Random(seed)
    reps = [[] for _ in range(n_keys)]
    for _ in range(boot):
        idx = [i for g in (rng.choice(games) for _ in games) for i in by[g]]
        bb = logit_fit([X[i] for i in idx], [y[i] for i in idx])
        if bb[1] > 0:
            for j in range(n_keys):
                reps[j].append(1000 * bb[2 + j] / bb[1])
    return [(eq[j], percentile(reps[j], 0.05), percentile(reps[j], 0.95)) for j in range(n_keys)], b


def epic_table(games, boot: int) -> tuple[list[dict], dict]:
    sw = epic_swings(games)
    keys = EPIC_KEYS
    X, y, gid = logit_samples(games, keys)
    per_type, b = gold_equiv(X, y, gid, len(keys), boot)
    # modelo com um peso só: épicos totais, com cada vastilarva valendo 1/3 (um acampamento = um épico)
    h = 2 + keys.index("HORDE")
    Xp = [[r[0], r[1], sum(r[2:]) - r[h] + r[h] / 3] for r in X]
    (pooled,), _ = gold_equiv(Xp, y, gid, 1, boot)
    n_ev = defaultdict(int)
    for s, _ in games:
        for o in s["events"]["objectives"]:
            if o["type"] in D.EPIC and o["killerTeam"] in (100, 200):
                n_ev[D.epic_key(o)] += 1
    rows = []
    for j, k in enumerate(keys):
        e, lo, hi = per_type[j]
        rows.append({"objetivo": EPIC_LABEL[k], "chave": k, "eventos": n_ev[k],
                     "salto2min_n": len(sw[k]), "salto2min_media": rnd(mean(sw[k])), "salto2min_mediana": rnd(percentile(sw[k], 0.5)),
                     "equivVitoria": rnd(e), "ic90": f"{rnd(lo)}..{rnd(hi)}" if lo is not None else ""})
    e, lo, hi = pooled
    rows.append({"objetivo": "qualquer épico (peso único; larva = 1/3)", "chave": "*", "eventos": sum(n_ev.values()),
                 "salto2min_n": sum(len(v) for v in sw.values()),
                 "salto2min_media": rnd(mean([x for v in sw.values() for x in v])),
                 "salto2min_mediana": rnd(percentile([x for v in sw.values() for x in v], 0.5)),
                 "equivVitoria": rnd(e), "ic90": f"{rnd(lo)}..{rnd(hi)}" if lo is not None else ""})
    log(f"regressão logística: {len(X)} amostras (minutos {','.join(map(str, LOGIT_MINUTES))}) de {len(set(gid))} partidas; "
        f"coef. do ouro = {b[1]:.3f} por mil")
    return rows, {"perType": {k: per_type[j] for j, k in enumerate(keys)}, "pooled": pooled, "events": dict(n_ev), "swings": sw}


def round_to(x, step=50):
    return int(step * round(x / step))


def choose_weights(info: dict) -> tuple[dict, int, dict]:
    """Peso padrão = equivalência de "um épico qualquer" (vastilarva = 1/3). Por tipo:
      - >= 100 eventos e IC90 acima de zero: a equivalência própria;
      - menos eventos, mas IC90 inteiro acima do padrão: o limite inferior do IC90 (conservador);
      - senão: o padrão (vastilarva: padrão/3, já que cada larva é um evento).
    Tudo arredondado a 50 e limitado a [0, 3000]."""
    pe, plo, phi = info["pooled"]
    default = round_to(max(0, min(3000, pe)))
    clip = lambda x: round_to(max(0, min(3000, x)))
    weights, why = {}, {}
    for k, (e, lo, hi) in info["perType"].items():
        n = info["events"].get(k, 0)
        ci = f"{rnd(e)}, IC90 {rnd(lo)}..{rnd(hi)}"
        if e is not None and lo is not None and n >= 100 and lo > 0:
            weights[k] = clip(e)
            why[k] = f"equivalência própria ({n} eventos; {ci})"
        elif e is not None and lo is not None and lo > default:
            weights[k] = clip(lo)
            why[k] = f"limite inferior do IC90 ({n} eventos; {ci})"
        elif k == "HORDE":
            weights[k] = round_to(default / 3, 10)
            why[k] = f"padrão/3 por larva ({n} eventos; própria sem efeito claro: {ci})"
        else:
            why[k] = f"padrão ({n} eventos; própria instável ou sem amostra: {ci})"
    return weights, default, why


# ----------------------------------------------------------------------------- GOOD/BAD
def score_rows(rows: list, P: dict) -> list[dict]:
    sc = [score_of(d, P["weights"], P["default"]) for d in rows]
    out = []
    for q in (0.10, 0.25, 1 / 3, 0.5, 2 / 3, 0.75, 0.90):
        out.append({"percentil": round(100 * q), "score": rnd(percentile(sc, q))})
    return out


def choose_cuts(rows: list, P: dict) -> tuple[int, int, list[dict]]:
    """GOOD/BAD = terços da distribuição do placar (arredondados a 50). Também mede, para cortes simétricos ±T, quanto
    a proporção de "ruim" até 15:00 separa vitórias de derrotas (AUC), só como checagem."""
    sc = [score_of(d, P["weights"], P["default"]) for d in rows]
    good, bad = round_to(percentile(sc, 2 / 3)), round_to(percentile(sc, 1 / 3))
    grid = []
    for T in (200, 300, 400, 500, 600, 800, 1000):
        Q = dict(P, good=T, bad=-T)
        per = game_table(rows, Q, 15 * 60000)
        share = {k: g["ruim"] / g["n"] for k, g in per.items() if g["n"]}
        a = auc([share[k] for k, g in per.items() if g["won"] is False and g["n"]],
                [share[k] for k, g in per.items() if g["won"] is True and g["n"]])
        frac = lambda v: pct(sum(1 for x in sc if verdict(x, T, -T) == v), len(sc))
        grid.append({"corte": f"±{T}", "pctBoa": frac("boa"), "pctNeutra": frac("neutra"), "pctRuim": frac("ruim"),
                     "aucDerrotaAte15": rnd(a, 2)})
    Q = dict(P, good=good, bad=bad)
    per = game_table(rows, Q, 15 * 60000)
    share = {k: g["ruim"] / g["n"] for k, g in per.items() if g["n"]}
    a = auc([share[k] for k, g in per.items() if g["won"] is False and g["n"]],
            [share[k] for k, g in per.items() if g["won"] is True and g["n"]])
    frac = lambda v: pct(sum(1 for x in sc if verdict(x, good, bad) == v), len(sc))
    grid.append({"corte": f"terços {bad}/{good}", "pctBoa": frac("boa"), "pctNeutra": frac("neutra"), "pctRuim": frac("ruim"),
                 "aucDerrotaAte15": rnd(a, 2)})
    return good, bad, grid


# ----------------------------------------------------------------------------- limiares
SCAN = {
    "vida baixa": (lambda d: d["state"].get("hp"), "<", (30, 40, 50, 60, 70)),
    "ouro no bolso": (lambda d: d["state"].get("bolso"), ">=", (800, 1000, 1300, 1600, 2000, 2500)),
    "lane níveis": (lambda d: (d["lane"] or {}).get("dNivel"), ">=", (1, 2, 3)),
    "lane vida": (lambda d: (d["lane"] or {}).get("dVida"), ">=", (10, 20, 30, 40, 50)),
    "jungle inimigo perto": (lambda d: (d["enemyJungle"] or {}).get("dist"), "<=", (1500, 2000, 3000, 4000, 5000)),
}


def threshold_table(rows: list, P: dict) -> list[dict]:
    out = []
    for cat, (val, op, cuts) in SCAN.items():
        el = [d for d in rows if eligible(cat, d) and val(d) is not None]
        for L in cuts:
            hit = (lambda d, L=L: val(d) < L) if op == "<" else (lambda d, L=L: val(d) >= L) if op == ">=" else (lambda d, L=L: val(d) <= L)
            c = compare(el, hit, P)
            out.append({"sinal": cat, "limiar": f"{op} {L}", "atual": "*" if L == {"vida baixa": D.LIMITS["vida"],
                        "ouro no bolso": D.LIMITS["bolso"], "lane níveis": D.LIMITS["laneNiveis"], "lane vida": D.LIMITS["laneVida"],
                        "jungle inimigo perto": D.LIMITS["jungleDist"]}[cat] else "",
                        "n": c["n"], "nSem": c["nSem"], "scoreCom": c["scoreCom"], "scoreSem": c["scoreSem"], "diff": c["diff"],
                        "ic95": c["ic95"], "diffNaPartida": c["diffNaPartida"], "icNaPartida": c["icNaPartida"],
                        "ruimCom": c["ruimCom"], "ruimSem": c["ruimSem"],
                        "conclusao": conclude(c)})
    return out


# ----------------------------------------------------------------------------- comandos
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("signals", "games", "epics", "score", "thresholds", "all"))
    ap.add_argument("-p", "--player", default="me")
    ap.add_argument("--role", help="só partidas em que o jogador jogou nesta role (ex.: JUNGLE)")
    ap.add_argument("--each", action="store_true", help="games: uma linha por partida")
    ap.add_argument("--boot", type=int, default=100, help="réplicas de bootstrap da regressão (epics/all)")
    ap.add_argument("--write", action="store_true", help="all: grava data/calibration.json")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-header", action="store_true")
    a = ap.parse_args()
    out = Out(a)
    games = load_games(a.player, a.role)
    if not games:
        raise SystemExit("Nenhuma partida de Summoner's Rift (> 10 min) com esse jogador no cache.")
    P = current_params()

    if a.cmd == "epics":
        for r in epic_table(games, a.boot)[0]:
            out.row(r)
        return
    rows = collect(games)
    if a.cmd == "signals":
        for r in signal_table(rows, P):
            out.row(r)
    elif a.cmd == "games":
        if a.each:
            for gid, g in game_table(rows, P).items():
                out.row({"gameId": gid, "resultado": "vitória" if g["won"] else "derrota", "decisoes": g["n"],
                         "ruim": g["ruim"], "boa": g["boa"], "pctRuim": pct(g["ruim"], g["n"])})
            return
        for lab, until in (("partida inteira", None), ("até 15:00", 15 * 60000)):
            for r in games_summary(game_table(rows, P, until), lab):
                out.row(r)
    elif a.cmd == "score":
        log(f"pesos atuais: {P['weights'] or '-'} (padrão {P['default']}); GOOD {P['good']}, BAD {P['bad']}")
        for r in score_rows(rows, P):
            out.row(r)
        for r in choose_cuts(rows, P)[2]:
            out.row(r) if a.json else log("\t".join(str(v) for v in r.values()))
    elif a.cmd == "thresholds":
        for r in threshold_table(rows, P):
            out.row(r)
    else:  # all
        etab, info = epic_table(games, a.boot)
        weights, default, why = choose_weights(info)
        P = {"weights": weights, "default": default, "good": 0, "bad": 0}
        good, bad, grid = choose_cuts(rows, P)
        P.update(good=good, bad=bad)
        sig = signal_table(rows, P)
        weak = sorted(r["sinal"] for r in sig if not r["conclusao"].startswith(("antecede pior", "amostra pequena")))
        gsum = games_summary(game_table(rows, P), "partida inteira") + games_summary(game_table(rows, P, 15 * 60000), "até 15:00")
        for k in EPIC_KEYS:
            out.row({"parametro": f"peso {EPIC_LABEL[k]}", "valor": weights.get(k, default), "origem": why[k]})
        out.row({"parametro": "peso padrão (EPIC_WEIGHT)", "valor": default,
                 "origem": f"equivalência de 1 épico qualquer na chance de vitória (IC90 {rnd(info['pooled'][1])}..{rnd(info['pooled'][2])})"})
        out.row({"parametro": "GOOD", "valor": good, "origem": "percentil 67 do placar das janelas"})
        out.row({"parametro": "BAD", "valor": bad, "origem": "percentil 33 do placar das janelas"})
        out.row({"parametro": "sinais fracos", "valor": ",".join(weak) or "-", "origem": "não antecederam placar pior (sem efeito, só tendência ou melhor)"})
        if a.write:
            cal = {"generatedBy": "tools/calibrate.py all --write", "date": date.today().isoformat(),
                   "player": a.player, "role": a.role, "games": len(games), "decisions": len(rows),
                   "epicWeight": default, "epicWeights": weights, "good": good, "bad": bad,
                   "weakSignals": weak, "limits": dict(D.LIMITS),
                   "why": {"epicWeights": why, "cuts": "terços da distribuição do placar das janelas"},
                   "epics": etab, "cutsGrid": grid, "signals": sig, "games_vs_verdict": gsum}
            save_json(D.CALIBRATION_FILE, cal, indent=1)
            log(f"gravado {D.CALIBRATION_FILE}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
