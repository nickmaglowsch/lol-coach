"""Utilitários compartilhados: caminhos, cliente LCU (API local) e cliente Riot (Match-V5).

Regras de segurança:
- A senha do lockfile e a RIOT_API_KEY NUNCA são impressas nem gravadas em disco.
- A verificação TLS é desativada SOMENTE para 127.0.0.1 (certificado autoassinado do cliente).
- A API local é usada SOMENTE para leitura (apenas GET).
- JSON baixado é tratado como dado; nada nele é executado.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MATCHES = DATA / "matches"
SUMMARIES = DATA / "summaries"
REPORTS = ROOT / "reports"

# Saída UTF-8 no console do Windows
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

LOCKFILE_CANDIDATES = [
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
    Path(r"C:\Program Files\Riot Games\League of Legends\lockfile"),
    Path(r"C:\Program Files (x86)\Riot Games\League of Legends\lockfile"),
]

QUEUE_NAMES = {
    400: "Normal Draft", 420: "Ranked Solo/Duo", 430: "Normal Blind", 440: "Ranked Flex",
    450: "ARAM", 490: "Quickplay", 700: "Clash", 900: "URF", 1700: "Arena", 1900: "URF",
}


class LCUError(Exception):
    pass


class RiotAPIError(Exception):
    pass


# ----------------------------------------------------------------------------- LCU
def find_lockfile() -> Path | None:
    env = os.environ.get("LOL_LOCKFILE")
    if env and Path(env).is_file():
        return Path(env)
    for p in LOCKFILE_CANDIDATES:
        if p.is_file():
            return p
    # Busca rasa em discos comuns
    for drive in string.ascii_uppercase[2:8]:
        base = Path(f"{drive}:\\")
        if not base.exists():
            continue
        for sub in ("Riot Games", "Games\\Riot Games", "Jogos\\Riot Games"):
            p = base / sub / "League of Legends" / "lockfile"
            if p.is_file():
                return p
    return None


class LCU:
    """Cliente mínimo, somente leitura, da API local do League Client."""

    def __init__(self) -> None:
        lf = find_lockfile()
        if lf is None:
            raise LCUError(
                "lockfile não encontrado. Abra o cliente do LoL e faça login "
                "(ou defina LOL_LOCKFILE com o caminho do lockfile)."
            )
        try:
            parts = lf.read_text(encoding="utf-8").strip().split(":")
        except OSError as e:
            raise LCUError(f"não consegui ler o lockfile ({e.__class__.__name__}).") from None
        if len(parts) != 5:
            raise LCUError("lockfile em formato inesperado.")
        _name, _pid, port, password, proto = parts
        self.base = f"{proto}://127.0.0.1:{port}"
        token = base64.b64encode(f"riot:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        del password, token
        # Contexto TLS sem verificação, usado EXCLUSIVAMENTE para 127.0.0.1
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str, params: dict | None = None, timeout: float = 20):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        assert urllib.parse.urlparse(url).hostname == "127.0.0.1"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read()).get("message", "")
            except Exception:
                pass
            raise LCUError(f"HTTP {e.code} em GET {path} {msg}".strip()) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise LCUError(
                f"cliente não respondeu em {path} ({e.__class__.__name__}). O cliente está aberto e logado?"
            ) from None
        return json.loads(body) if body else None

    def get_bytes(self, path: str, timeout: float = 20) -> bytes:
        """GET binário (imagens do cliente). Somente leitura, só 127.0.0.1."""
        url = self.base + path
        assert urllib.parse.urlparse(url).hostname == "127.0.0.1"
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise LCUError(f"HTTP {e.code} em GET {path}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise LCUError(f"cliente não respondeu em {path} ({e.__class__.__name__}).") from None


# ----------------------------------------------------------------------------- Riot API
class RiotAPI:
    """Cliente mínimo da API oficial (Match-V5). Chave lida de RIOT_API_KEY."""

    def __init__(self, routing: str = "americas") -> None:
        key = os.environ.get("RIOT_API_KEY", "").strip()
        if not key:
            raise RiotAPIError(
                "RIOT_API_KEY não definida. Gere uma chave em https://developer.riotgames.com "
                "(login com a conta Riot -> 'Development API Key', válida por 24h) e rode, no PowerShell:\n"
                "  $env:RIOT_API_KEY = 'RGAPI-...'"
            )
        self._key = key
        self.routing = routing

    def get(self, host: str, path: str, params: dict | None = None):
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Riot-Token": self._key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RiotAPIError(f"HTTP {e.code} em {path}") from None

    def account_by_riot_id(self, name: str, tag: str):
        return self.get(self.routing, f"/riot/account/v1/accounts/by-riot-id/"
                        f"{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}")

    def match_ids(self, puuid: str, queue: int | None = None, start: int = 0, count: int = 20):
        p = {"start": start, "count": count}
        if queue is not None:
            p["queue"] = queue
        return self.get(self.routing, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", p)

    def match(self, match_id: str):
        return self.get(self.routing, f"/lol/match/v5/matches/{match_id}")

    def timeline(self, match_id: str):
        return self.get(self.routing, f"/lol/match/v5/matches/{match_id}/timeline")


# ----------------------------------------------------------------------------- IO
def load_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(p: Path, obj, indent: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    tmp.replace(p)


# ----------------------------------------------------------------------------- helpers de análise
STATIC = DATA / "static"


def mmss(ms: float) -> str:
    s = int(round(ms / 1000))
    return f"{s // 60:02d}:{s % 60:02d}"


def parse_time(t) -> int:
    """'15:31' / '15' / 931000 -> milissegundos."""
    if isinstance(t, (int, float)):
        return int(t)
    t = str(t).strip()
    if ":" in t:
        m, s = t.split(":", 1)
        return (int(m) * 60 + int(s)) * 1000
    return int(float(t) * 60000)


def queue_name(qid) -> str:
    if qid in QUEUE_NAMES:
        return QUEUE_NAMES[qid]
    p = STATIC / "queues.json"
    if p.exists():
        for q in load_json(p):
            if q["id"] == qid and q.get("name"):
                return q["name"]
    return str(qid)


def me_puuid() -> str | None:
    p = DATA / "me.json"
    return load_json(p).get("puuid") if p.exists() else None


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def resolve_player(participants: list[dict], sel: str | int | None) -> dict | None:
    """Acha um participante por: 'me' (conta do cliente), pid, Riot ID 'Nome#TAG', nome, campeão.
    Comparação ignora maiúsculas, espaços e pontuação (Cho'Gath == chogath)."""
    if sel is None or str(sel).lower() == "me":
        me = me_puuid()
        return next((p for p in participants if me and p.get("puuid") == me), None)
    s = str(sel)
    if s.isdigit() and int(s) <= 10:
        return next((p for p in participants if p["pid"] == int(s)), None)
    n = _norm(s)
    for key in (lambda p: _norm(p["riotId"]), lambda p: _norm(p["riotId"].split("#")[0]), lambda p: _norm(p["champion"])):
        hit = [p for p in participants if key(p) == n]
        if len(hit) == 1:
            return hit[0]
    return None
