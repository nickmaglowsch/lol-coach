"""Blocos de relatório usados pelos dois renders (uma partida e várias partidas).

Cada função recebe o spec (ou uma lista) e devolve um trecho de HTML ou linhas de Markdown.
Corrigir um bloco aqui corrige os dois tipos de relatório.
"""
from __future__ import annotations

import html
import re

ZONE_LABEL = {"top": "na top", "mid": "na mid", "bot": "no bot", "river": "no rio", "jungle_ally": "na nossa jungle",
              "jungle_enemy": "na jungle dele", "base_ally": "na nossa base", "base_enemy": "na base dele"}
VERDICTS = {"SUPORTADO": "ok", "REFUTADO": "bad", "INCONCLUSIVO": "unk", "MISTO": "unk"}
TIME_RE = re.compile(r"(?<![\w:])(\d{1,2}:\d{2}(?:\s*[–-]\s*\d{1,2}:\d{2})?)(?![\w:])")


def E(x) -> str:
    return html.escape(str(x))


def mk(text) -> str:
    """Markup mínimo e seguro: escapa HTML, **negrito**, horários -> <time>."""
    t = html.escape(str(text), quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return TIME_RE.sub(r"<time>\1</time>", t)


def _section(eyebrow: str, title: str, inner: str) -> str:
    return f'<section><div class="eyebrow">{E(eyebrow)}</div><h2>{mk(title)}</h2>{inner}</section>'


# ----------------------------------------------------------------------------- HTML
def header(eyebrow: str, title: str, tiles: list[tuple[str, str]], sub_html: str) -> str:
    """tiles: [(html do número já escapado, legenda)]; sub_html já escapado."""
    score = "".join(f"<div>{b}<span>{t}</span></div>" for b, t in tiles)
    return (f'<header>\n  <div class="eyebrow">{E(eyebrow)}</div>\n  <h1>{mk(title)}</h1>\n'
            f'  <div class="score">{score}</div>\n  <p class="sub">{sub_html}</p>\n</header>')


VERDICT_LEGEND = ('recebe um veredito: <strong style="color:var(--ok)">suportado</strong>, '
                  '<strong style="color:var(--bad)">refutado</strong> ou <strong style="color:var(--unk)">inconclusivo</strong>')


def note(spec: dict) -> str:
    return f'<div class="note">{mk(spec["note"])}</div>' if spec.get("note") else ""


def tldr(spec: dict) -> str:
    if not spec.get("tldr"):
        return ""
    cards = []
    for n, t in enumerate(spec["tldr"], 1):
        title, text = (t.get("title", ""), t.get("text", "")) if isinstance(t, dict) else ("", t)
        head = f"<h3>{mk(title)}</h3>" if title else ""
        cards.append(f'<article class="card"><span class="card-n">{n}</span>'
                     f'<div class="card-body">{head}<p>{mk(text)}</p></div></article>')
    return _section("Em 30 segundos", "Resumo", '<div class="cards">' + "".join(cards) + "</div>")


def verdicts(spec: dict, eyebrow: str, title: str) -> str:
    if not spec.get("verdicts"):
        return ""
    cards = []
    for n, v in enumerate(spec["verdicts"], 1):
        vv = v["verdict"].upper()
        points = "".join(f"<li>{mk(p)}</li>" for p in v.get("points", []))
        cards.append(f'<article class="v"><div class="v-head"><h3>{n}. {mk(v["q"])}</h3>'
                     f'<span class="chip {VERDICTS[vv]}">{E(v.get("label") or vv.capitalize())}</span></div>\n'
                     f'<ul>{points}</ul></article>')
    return _section(eyebrow, title, '<div class="verdicts">' + "".join(cards) + "</div>")


def sections(spec: dict) -> list[str]:
    out = []
    for sec in spec.get("sections", []):
        items = "".join(f"<li><span>{mk(p)}</span></li>" for p in sec.get("points", []))
        out.append(_section(sec.get("eyebrow", "Detalhe"), sec["title"], f'<ul class="plain">{items}</ul>'))
    return out


def recs(spec: dict, eyebrow: str) -> str:
    if not spec.get("recs"):
        return ""
    items = "".join(f'<li><div><strong>{mk(r["title"])}</strong> {mk(r.get("text", ""))}</div></li>' for r in spec["recs"])
    return _section(eyebrow, "Recomendações", f'<ol class="recs">{items}</ol>')


def limits(items: list[str]) -> str:
    lis = "".join(f"<li>{mk(l)}</li>" for l in items)
    return f'<section><div class="eyebrow">O que os dados não mostram</div><ul class="limits">{lis}</ul></section>'


# ----------------------------------------------------------------------------- Markdown
def md_intro(spec: dict) -> list[str]:
    out = []
    if spec.get("artifactUrl"):
        out += [f'Versão visual: {spec["artifactUrl"]}', ""]
    if spec.get("note"):
        out += [f'> {spec["note"]}', ""]
    out += ["## Resumo em 5 linhas"] + [f"{i}. " + (f'**{t["title"]}:** {t["text"]}' if isinstance(t, dict) else t)
                                        for i, t in enumerate(spec.get("tldr", []), 1)] + [""]
    return out


def md_verdicts(spec: dict, heading: str) -> list[str]:
    if not spec.get("verdicts"):
        return []
    out = [f"## {heading}"]
    for n, v in enumerate(spec["verdicts"], 1):
        out += [f'### {n}. {v["q"]} → **{v.get("label") or v["verdict"].upper()}**'] + [f"- {p}" for p in v.get("points", [])] + [""]
    return out


def md_tail(spec: dict, lim: list[str], lim_heading: str) -> list[str]:
    out = []
    for sec in spec.get("sections", []):
        out += [f'## {sec["title"]}'] + [f"- {p}" for p in sec.get("points", [])] + [""]
    if spec.get("recs"):
        out += ["## Recomendações (máx. 3)"] + [f'{i}. **{r["title"]}** {r.get("text", "")}' for i, r in enumerate(spec["recs"], 1)] + [""]
    out += [f"## {lim_heading}"] + [f"- {l}" for l in lim] + [""]
    return out
