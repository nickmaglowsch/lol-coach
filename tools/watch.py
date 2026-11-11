"""Vigia o histórico do cliente e prepara a análise de cada partida nova.

  python tools/watch.py [--interval 120] [--queue 420,440] [--tema review] [--once]

A cada N segundos lê o histórico pela API local do cliente (somente GET; as mesmas funções do `lol list`).
Partida nova = está no histórico, não é remake, ainda não está no cache e é mais recente que a última partida do
histórico que já está no cache (não baixa partidas antigas: para isso use `lol fetch-many`; com o cache vazio,
pega só a mais recente). Para cada uma, da mais antiga para a mais nova: `lol fetch <id>` e `lol new <id>`
(esqueleto do spec em reports/), e imprime o caminho do spec no stdout (uma linha por partida).
Durante a partida o histórico não muda, então ele só continua lendo. Sem notificações: mensagens no stderr.
Cliente fechado ou deslogado: avisa e tenta de novo no próximo ciclo (com --once, só avisa e sai com código 0).
Ctrl+C encerra.
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

from common import LCU, MATCHES, LCUError
from fetch import fmt_game, list_lcu

TOOLS = Path(__file__).resolve().parent
MAX_TRIES = 3   # tentativas de fetch por partida (a timeline às vezes demora a ficar disponível depois do fim)


def log(msg: str) -> None:
    print(f"[watch {dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def cached(gid) -> bool:
    d = MATCHES / str(gid)
    return (d / "timeline.json").exists() or (d / "timeline_v5.json").exists()


def connect() -> tuple[LCU | None, str]:
    """Cliente logado (somente leitura) ou None com o motivo. Não imprime nada: quem chama decide quando avisar."""
    try:
        lcu = LCU()
        lcu.get("/lol-summoner/v1/current-summoner")
        return lcu, ""
    except LCUError as e:
        return None, str(e)


def new_games(games: list[dict], queues: set[int] | None) -> list[dict]:
    games = sorted((g for g in games if not g.get("remake") and (not queues or g["queueId"] in queues)),
                   key=lambda g: g["creation"] or 0)
    have = [g["creation"] or 0 for g in games if cached(g["gameId"])]
    if not have:
        return games[-1:]
    return [g for g in games if not cached(g["gameId"]) and (g["creation"] or 0) > max(have)]


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, str(TOOLS / script), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stderr.strip():
        sys.stderr.write(r.stderr if r.stderr.endswith("\n") else r.stderr + "\n")
    return r


def process(g: dict, tema: str) -> bool:
    """fetch + esqueleto do spec. False = o fetch falhou (tenta de novo no próximo ciclo)."""
    gid = str(g["gameId"])
    log("partida nova: " + fmt_game(g))
    r = run("fetch.py", "fetch", gid)
    if r.returncode or not cached(gid):
        log(f"não consegui baixar {gid}; tento de novo no próximo ciclo")
        return False
    r = run("scaffold.py", "new", gid, "--tema", tema)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if r.returncode or not lines:
        log(f"{gid} baixada, mas o esqueleto falhou; rode `lol new {gid}` à mão")
        return True
    print(lines[-1], flush=True)   # caminho do spec
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=120, help="segundos entre leituras do histórico (mín. 15)")
    ap.add_argument("--queue", help="filas separadas por vírgula (ex.: 420,440); padrão: todas")
    ap.add_argument("--tema", default="review", help="tema do spec criado pelo `lol new`")
    ap.add_argument("--once", action="store_true", help="lê o histórico uma vez, processa o que for novo e sai")
    a = ap.parse_args()
    try:
        queues = {int(q) for q in a.queue.split(",") if q.strip()} if a.queue else None
    except ValueError:
        ap.error("--queue espera números separados por vírgula (ex.: 420,440)")
    interval = max(15, a.interval)
    tries: dict[int, int] = {}
    last_status = None
    if not a.once:
        log(f"vigiando o histórico a cada {interval}s" + (f" (filas {sorted(queues)})" if queues else "") + "; Ctrl+C encerra")
    try:
        while True:
            lcu, why = connect()
            if lcu is None:
                status = "cliente do LoL fechado ou deslogado (" + why.rstrip(".") + ")"
                if a.once or status != last_status:
                    log(status + ("." if a.once else f"; tento de novo a cada {interval}s"))
            else:
                try:
                    todo = [g for g in new_games(list_lcu(lcu), queues) if tries.get(g["gameId"], 0) < MAX_TRIES]
                    status = "nenhuma partida nova" if not todo else ""
                    if status and (a.once or status != last_status):
                        log(status + ("" if a.once else "; esperando a próxima"))
                    for g in todo:
                        if not process(g, a.tema):
                            tries[g["gameId"]] = tries.get(g["gameId"], 0) + 1
                            if tries[g["gameId"]] >= MAX_TRIES:
                                log(f'desisti de {g["gameId"]} depois de {MAX_TRIES} tentativas; use `lol fetch {g["gameId"]}`')
                except LCUError as e:
                    status = f"histórico indisponível ({e})"
                    if a.once or status != last_status:
                        log(status)
            last_status = status
            if a.once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        log("encerrado")


if __name__ == "__main__":
    main()
