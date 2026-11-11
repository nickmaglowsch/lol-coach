"""Obtenção de dados de partidas.

Uso:
  python tools/fetch.py list [--queue 440] [--n 20] [--source auto|lcu|riot] [--player "Nome#TAG"]
  python tools/fetch.py fetch <gameId|latest|latest-flex|latest-solo> [--source auto|lcu|riot] [--force]
  python tools/fetch.py fetch-many [--queue 440] [--n 10] [--source auto|lcu|riot]
  python tools/fetch.py static        # baixa/atualiza nomes de campeões, itens e filas do cliente

Fontes:
  lcu  = API local do cliente (precisa do cliente aberto e logado). Fonte principal.
  riot = API oficial Match-V5 (precisa de RIOT_API_KEY). Fallback e/ou dados mais ricos
         (a timeline da Match-V5 tem wards, compras de itens, level-ups; a do LCU não).
  auto = LCU; se RIOT_API_KEY existir, também baixa a versão Match-V5 (arquivos *_v5.json).

Cache: data/matches/<gameId>/{match.json,timeline.json,match_v5.json,timeline_v5.json,meta.json}.
Nada é baixado de novo se o arquivo já existir (use --force para sobrescrever).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from common import (DATA, MATCHES, LCU, LCUError, RiotAPI, RiotAPIError, load_json,
                    queue_name, save_json)

ME_FILE = DATA / "me.json"  # puuid / riot id / plataforma da conta (sem segredos)
STATIC = DATA / "static"

PLATFORM_ROUTING = {
    "BR1": "americas", "NA1": "americas", "LA1": "americas", "LA2": "americas",
    "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe", "ME1": "europe",
    "KR": "asia", "JP1": "asia",
    "OC1": "sea", "PH2": "sea", "SG2": "sea", "TH2": "sea", "TW2": "sea", "VN2": "sea",
}
QUEUE_ALIASES = {"latest-flex": 440, "latest-solo": 420, "latest": None}


class FetchError(Exception):
    pass


# ----------------------------------------------------------------------------- helpers
def champ_names() -> dict[int, str]:
    p = STATIC / "champions.json"
    if p.exists():
        return {int(c["id"]): c["name"] for c in load_json(p)}
    return {}


def ensure_static(lcu: LCU | None, force: bool = False) -> None:
    targets = {
        "champions.json": "/lol-game-data/assets/v1/champion-summary.json",
        "items.json": "/lol-game-data/assets/v1/items.json",
        "queues.json": "/lol-game-data/assets/v1/queues.json",
    }
    if lcu is None:
        return
    for fname, path in targets.items():
        p = STATIC / fname
        if p.exists() and not force:
            continue
        try:
            data = lcu.get(path)
            if fname == "items.json":  # só o que interessa
                data = [{"id": i["id"], "name": i["name"], "priceTotal": i.get("priceTotal"),
                         "categories": i.get("categories", [])} for i in data]
            elif fname == "champions.json":
                data = [{"id": c["id"], "name": c["name"], "alias": c.get("alias")} for c in data]
            elif fname == "queues.json":
                data = [{"id": q["id"], "name": q.get("name"), "description": q.get("description")} for q in data]
            save_json(p, data)
        except LCUError as e:
            print(f"[aviso] dados estáticos {fname}: {e}", file=sys.stderr)


ICONS = STATIC / "champion-icons"


def game_champion_ids(game_id: int) -> list[int]:
    d = MATCHES / str(game_id)
    if (d / "match.json").exists():
        return [p["championId"] for p in load_json(d / "match.json")["participants"]]
    if (d / "match_v5.json").exists():
        return [p["championId"] for p in load_json(d / "match_v5.json")["info"]["participants"]]
    return []


def ensure_icons(lcu: LCU | None, champ_ids) -> int:
    """Baixa (uma vez) o retrato quadrado de cada campeão pela API local do cliente."""
    if lcu is None:
        return 0
    got = 0
    for cid in set(champ_ids):
        p = ICONS / f"{cid}.png"
        if p.exists() or cid <= 0:
            continue
        try:
            data = lcu.get_bytes(f"/lol-game-data/assets/v1/champion-icons/{cid}.png")
        except LCUError as e:
            print(f"[aviso] ícone {cid}: {e}", file=sys.stderr)
            continue
        if data[1:4] != b"PNG":  # assinatura PNG (o 1o byte e 0x89)
            print(f"[aviso] ícone {cid}: resposta não é PNG", file=sys.stderr)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        got += 1
    return got


def save_me(lcu: LCU) -> dict:
    me = lcu.get("/lol-summoner/v1/current-summoner")
    info = {"puuid": me["puuid"], "gameName": me.get("gameName"), "tagLine": me.get("tagLine"),
            "riotId": f'{me.get("gameName")}#{me.get("tagLine")}'}
    try:  # platformId real (ex.: BR1) vem do histórico; region-locale devolve só "BR"
        h = lcu.get("/lol-match-history/v1/products/lol/current-summoner/matches")
        info["platformId"] = h.get("platformId") or None
    except LCUError:
        pass
    old = load_json(ME_FILE) if ME_FILE.exists() else {}
    old.update({k: v for k, v in info.items() if v})
    save_json(ME_FILE, old, indent=2)
    return old


def get_lcu() -> LCU | None:
    try:
        lcu = LCU()
        lcu.get("/lol-summoner/v1/current-summoner")
        return lcu
    except LCUError as e:
        print(f"[LCU indisponível] {e}", file=sys.stderr)
        return None


def get_riot(platform: str | None = None) -> RiotAPI | None:
    try:
        return RiotAPI(PLATFORM_ROUTING.get((platform or "BR1").upper(), "americas"))
    except RiotAPIError:
        return None


def resolve_puuid(riot: RiotAPI, player: str | None) -> str:
    if player:
        if "#" not in player:
            raise SystemExit('Use o formato "Nome#TAG".')
        name, tag = player.rsplit("#", 1)
        return riot.account_by_riot_id(name, tag)["puuid"]
    if ME_FILE.exists():
        me = load_json(ME_FILE)
        # A API oficial usa um PUUID criptografado por chave, diferente do PUUID do cliente:
        # resolve pelo Riot ID e guarda em me.json como puuidApi.
        if me.get("puuidApi"):
            return me["puuidApi"]
        if me.get("gameName") and me.get("tagLine"):
            puuid = riot.account_by_riot_id(me["gameName"], me["tagLine"])["puuid"]
            me["puuidApi"] = puuid
            save_json(ME_FILE, me, indent=2)
            return puuid
    raise SystemExit("Não sei quem é o jogador: abra o cliente (LCU) uma vez ou passe --player \"Nome#TAG\".")


# ----------------------------------------------------------------------------- listagem
def list_lcu(lcu: LCU) -> list[dict]:
    """Lista o histórico pelo LCU. Observado: o cliente ignora begIndex/endIndex e devolve
    sempre as 20 partidas mais recentes."""
    h = lcu.get("/lol-match-history/v1/products/lol/current-summoner/matches",
                {"begIndex": 0, "endIndex": 99})
    names = champ_names()
    out = []
    for g in h.get("games", {}).get("games", []):
        p = g["participants"][0]
        s = p.get("stats", {})
        out.append({
            "gameId": g["gameId"], "platformId": g.get("platformId"), "queueId": g.get("queueId"),
            "creation": g.get("gameCreation"), "duration": g.get("gameDuration"),
            "champion": names.get(p.get("championId"), str(p.get("championId"))),
            "k": s.get("kills"), "d": s.get("deaths"), "a": s.get("assists"),
            "win": s.get("win"), "remake": g.get("endOfGameResult") == "Abort_AntiCheatExit" or
            (g.get("gameDuration", 999) < 300), "source": "lcu",
        })
    return out


def list_riot(riot: RiotAPI, puuid: str, queue: int | None, n: int, platform: str) -> list[dict]:
    out, start = [], 0
    while len(out) < n:
        ids = riot.match_ids(puuid, queue=queue, start=start, count=min(100, n - len(out)))
        if not ids:
            break
        for mid in ids:
            gid = int(mid.split("_", 1)[1])
            cached = MATCHES / str(gid) / "match_v5.json"
            m = load_json(cached) if cached.exists() else riot.match(mid)
            if not cached.exists():
                save_json(cached, m)
            info = m["info"]
            me = next(p for p in info["participants"] if p["puuid"] == puuid)
            out.append({
                "gameId": gid, "platformId": info.get("platformId", platform), "queueId": info.get("queueId"),
                "creation": info.get("gameCreation"), "duration": info.get("gameDuration"),
                "champion": champ_names().get(me.get("championId")) or me.get("championName"),
                "k": me.get("kills"), "d": me.get("deaths"),
                "a": me.get("assists"), "win": me.get("win"),
                "remake": me.get("gameEndedInEarlySurrender", False), "source": "riot",
            })
            time.sleep(0.05)
        start += len(ids)
    return out


def get_list(args, need: int, warn: bool = True) -> list[dict]:
    lcu = None if args.source == "riot" else get_lcu()
    platform = "BR1"
    games: list[dict] = []
    if lcu:
        ensure_static(lcu)
        me = save_me(lcu)
        platform = me.get("platformId") or platform
        games = list_lcu(lcu)
        if args.queue is not None:
            games = [g for g in games if g["queueId"] == args.queue]
    if args.source == "riot" or (len(games) < need):
        riot = get_riot(platform)
        if riot:
            puuid = resolve_puuid(riot, getattr(args, "player", None))
            extra = list_riot(riot, puuid, args.queue, need, platform)
            seen = {g["gameId"] for g in games}
            games += [g for g in extra if g["gameId"] not in seen]
        elif args.source == "riot":
            RiotAPI()  # levanta a mensagem explicando como gerar a chave
        elif len(games) < need and warn:
            print(f"[info] O LCU só devolve as 20 partidas mais recentes ({len(games)} nesta fila). "
                  "Para ir além, defina RIOT_API_KEY (veja CLAUDE.md).", file=sys.stderr)
    games.sort(key=lambda g: g["creation"] or 0, reverse=True)
    return games[:need]


def fmt_game(g: dict) -> str:
    when = dt.datetime.fromtimestamp((g["creation"] or 0) / 1000).strftime("%Y-%m-%d %H:%M")
    dur = f'{(g["duration"] or 0) // 60}:{(g["duration"] or 0) % 60:02d}'
    res = "REMAKE" if g.get("remake") else ("V" if g["win"] else "D")
    cached = "✓" if (MATCHES / str(g["gameId"]) / "timeline.json").exists() or \
        (MATCHES / str(g["gameId"]) / "timeline_v5.json").exists() else " "
    q = queue_name(g["queueId"])[:16]
    kda = f'{g["k"]}/{g["d"]}/{g["a"]}'
    return (f'{g["gameId"]:<12} {when}  {q:<16} {str(g["champion"])[:14]:<14} '
            f'{kda:<8} {res:<6} {dur:>6}  {cached}')


def cmd_list(args) -> None:
    games = get_list(args, args.n)
    if args.ids:
        for g in games:
            if not g.get("remake"):
                print(g["gameId"])
        return
    print(f'{"gameId":<12} {"data":<17} {"fila":<16} {"campeão":<14} {"KDA":<8} {"res":<6} {"dur":>6}  cache')
    for g in games:
        print(fmt_game(g))
    if not games:
        print("(nenhuma partida encontrada)")


# ----------------------------------------------------------------------------- download
def fetch_game(game_id: int, platform: str, source: str, force: bool, lcu: LCU | None) -> dict:
    d = MATCHES / str(game_id)
    meta_p = d / "meta.json"
    meta = load_json(meta_p) if meta_p.exists() else {"gameId": game_id, "platformId": platform}
    got = []

    if source in ("auto", "lcu") and lcu:
        for fname, path in (("match.json", f"/lol-match-history/v1/games/{game_id}"),
                            ("timeline.json", f"/lol-match-history/v1/game-timelines/{game_id}")):
            if (d / fname).exists() and not force:
                continue
            for attempt in range(2):
                try:
                    save_json(d / fname, lcu.get(path))
                    got.append(fname)
                    break
                except LCUError as e:
                    if attempt == 1:
                        print(f"[LCU] {game_id} {fname}: {e}", file=sys.stderr)
                    else:
                        time.sleep(1.5)
        meta["lcu"] = (d / "timeline.json").exists()

    if source in ("auto", "riot"):
        riot = get_riot(platform)
        if riot is None:
            if source == "riot" or not (d / "timeline.json").exists():
                print("[Riot API] RIOT_API_KEY não definida; pulando Match-V5. Veja CLAUDE.md.", file=sys.stderr)
        else:
            mid = f"{platform}_{game_id}"
            for fname, fn in (("match_v5.json", riot.match), ("timeline_v5.json", riot.timeline)):
                if (d / fname).exists() and not force:
                    continue
                try:
                    save_json(d / fname, fn(mid))
                    got.append(fname)
                except RiotAPIError as e:
                    print(f"[Riot API] {fname}: {e}", file=sys.stderr)
            meta["riot"] = (d / "timeline_v5.json").exists()

    if not any((d / f).exists() for f in ("timeline.json", "timeline_v5.json")):
        raise FetchError(f"Não consegui a timeline de {game_id} por nenhuma fonte.")
    ensure_icons(lcu, game_champion_ids(game_id))
    meta["fetchedAt"] = meta.get("fetchedAt") or dt.datetime.now().isoformat(timespec="seconds")
    save_json(meta_p, meta, indent=2)
    status = ("baixado: " + ", ".join(got)) if got else "já estava em cache"
    print(f"{game_id}: {status}", file=sys.stderr)
    print(game_id)  # stdout só com o id, para encadear (ex.: lol fetch latest-flex | lol summarize -)
    return meta


def cmd_fetch(args) -> None:
    lcu = None if args.source == "riot" else get_lcu()
    platform = "BR1"
    if lcu:
        ensure_static(lcu)
        platform = save_me(lcu).get("platformId") or platform
    elif (DATA / "me.json").exists():
        platform = load_json(DATA / "me.json").get("platformId") or platform
    targets = []
    for t in args.game:
        if t == "-":
            targets += [ln.split()[0] for ln in sys.stdin if ln.strip() and not ln.startswith("#")]
        else:
            targets.append(t)
    failed = False
    for target in targets:
        if target in QUEUE_ALIASES:
            args.queue = QUEUE_ALIASES[target]
            games = [g for g in get_list(args, 20, warn=False) if not g.get("remake")]
            if not games:
                raise SystemExit(f"Nenhuma partida encontrada para '{target}'.")
            gid = games[0]["gameId"]
            print("Partida escolhida: " + fmt_game(games[0]), file=sys.stderr)
        else:
            gid = int(str(target).split("_")[-1])
        try:
            fetch_game(gid, platform, args.source, args.force, lcu)
        except FetchError as e:
            failed = True
            print(f"[falhou] {e}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


def cmd_fetch_many(args) -> None:
    games = [g for g in get_list(args, args.n) if not g.get("remake")]
    lcu = None if args.source == "riot" else get_lcu()
    failed = []
    for g in games:
        try:
            fetch_game(g["gameId"], g.get("platformId") or "BR1", args.source, args.force, lcu)
        except FetchError as e:
            failed.append(g["gameId"])
            print(f"[falhou] {e}", file=sys.stderr)
    if failed:
        print(f"{len(failed)} partida(s) sem timeline: {failed}", file=sys.stderr)


def cmd_icons(args) -> None:
    lcu = get_lcu()
    if not lcu:
        raise SystemExit("Precisa do cliente aberto para baixar os ícones.")
    ids = []
    for g in args.games:
        if g == "-":
            ids += [ln.split()[0] for ln in sys.stdin if ln.strip() and not ln.startswith("#")]
        else:
            ids.append(g)
    if not ids:
        ids = [d.name for d in MATCHES.iterdir() if d.is_dir() and d.name.isdigit()]
    n = sum(ensure_icons(lcu, game_champion_ids(int(str(g).split("_")[-1]))) for g in ids)
    print(f"{n} ícone(s) novo(s) em {ICONS.relative_to(DATA.parent)}", file=sys.stderr)


def cmd_static(args) -> None:
    lcu = get_lcu()
    if not lcu:
        raise SystemExit("Precisa do cliente aberto.")
    ensure_static(lcu, force=True)
    print("OK:", ", ".join(p.name for p in STATIC.glob("*.json")))


def main() -> None:
    ap = argparse.ArgumentParser(description="Baixa partidas de LoL (LCU local ou Riot Match-V5).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "fetch", "fetch-many"):
        sp = sub.add_parser(name)
        sp.add_argument("--source", choices=["auto", "lcu", "riot"], default="auto")
        sp.add_argument("--player", help='Riot ID "Nome#TAG" (só para a fonte riot)')
        sp.add_argument("--queue", type=int, default=None, help="queueId (440 Flex, 420 Solo, 400 Normal, 450 ARAM)")
        sp.add_argument("--force", action="store_true", help="baixa de novo mesmo com cache")
        if name == "fetch":
            sp.add_argument("game", nargs="+", help="gameId(s), latest, latest-flex, latest-solo ou '-' (stdin)")
        else:
            sp.add_argument("--n", type=int, default=20 if name == "list" else 10)
        if name == "list":
            sp.add_argument("--ids", action="store_true", help="só os gameIds (sem remakes), um por linha")
    sub.add_parser("static")
    sp = sub.add_parser("icons", help="baixa os retratos dos campeões das partidas (padrão: todas em cache)")
    sp.add_argument("games", nargs="*")
    args = ap.parse_args()
    try:
        {"list": cmd_list, "fetch": cmd_fetch, "fetch-many": cmd_fetch_many, "static": cmd_static,
         "icons": cmd_icons}[args.cmd](args)
    except (RiotAPIError, LCUError) as e:
        raise SystemExit(f"Erro: {e}")


if __name__ == "__main__":
    main()
