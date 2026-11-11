# lol-coach

Coach de League of Legends baseado nos **dados da timeline** das suas partidas, feito para usar com o [Claude Code](https://claude.com/claude-code). Você pergunta ("o jungle abandonou o top?", "onde a vantagem caiu?") e a análise responde com **minuto e dado**, classificando cada conclusão como **suportado**, **refutado** ou **inconclusivo**. O resultado sai como relatório em Markdown e como uma página visual com **replay interativo** da partida, que pode ser publicada como Claude Artifact e compartilhada.

*English: a timeline-based League of Legends coach for Claude Code. Pulls your matches from the local League client, builds per-minute summaries (zones, gold/XP/CS diffs, kills, objectives) and renders evidence-based reports with an interactive match replay. Docs and output are in Portuguese.*

## O que tem

- **Dados da partida** pela API local do cliente do LoL (LCU), só leitura. Também funciona com a API oficial Match-V5 (`RIOT_API_KEY`), que traz wards e compras de itens.
- **Resumo por minuto** dos 10 jogadores: zona do mapa (top, mid, bot, rio, jungle aliada/inimiga, base), ouro, XP, CS e diferenças contra o oponente direto, kills com local e quem estava perto, objetivos, recalls inferidos e limitações declaradas.
- **Consultas encadeáveis** (filosofia Unix, TSV ou JSON Lines): `find`, `frames`, `events`, `at`, `series`, `where`.
- **Relatório** gerado a partir de um "spec" com o julgamento da análise. Os números sempre vêm dos dados.
- **Revisão de decisões** momento a momento (foi para a lane, atravessou o mapa, objetivo, recall, morte): estado na hora, sinais, resultado e veredito, com cortes e pesos **calibrados no seu histórico** (`lol calibrate`) e a **chance de vitória** antes → depois de um modelo treinado em partidas do seu elo (`lol winprob`).
- **Visão antes dos objetivos** (`lol vision`), **padrão do jungle inimigo** por campeão (`lol junglers`) e **painel de evolução** com metas (`lol dashboard`).
- **Página visual**: resumo em cards, gráfico de ouro, vereditos, replay com retratos dos campeões no minimapa, torres caindo no horário real, waves de minions (simulação, com checkbox), linha do tempo clicável, mapas das lutas e timing de objetivos.

## Requisitos

- Windows com o League of Legends instalado. Para buscar partidas e retratos, o cliente precisa estar **aberto e logado**.
- Python 3.10+, só biblioteca padrão. O lançador `lol` procura `python` no PATH ou o Python do [mise](https://mise.jdx.dev); defina `LOL_PYTHON` para usar outro.
- Opcional: [Claude Code](https://claude.com/claude-code). O `CLAUDE.md` traz todas as instruções de coach (regras de honestidade, formato de relatório e tipos de análise).

## Uso rápido

```bash
./lol list --queue 440 --n 20                 # suas últimas partidas (440 = Ranked Flex)
./lol fetch latest-flex                       # baixa a última Flex (cache em data/)
./lol summarize latest                        # resumo legível no terminal + data/summaries/<id>.json
./lol summarize --player "Nome#TAG" --games 10 --queue 440   # padrões em várias partidas
```

No PowerShell use `.\lol` (é o `lol.cmd`). Depois de mexer no código, rode `./lol selftest`: ele testa tudo sem rede usando as partidas em cache. Para ir além das 20 partidas mais recentes, o cliente não pagina: use a API oficial com `RIOT_API_KEY` (veja o `CLAUDE.md`).

### Consultas

```bash
./lol find --enemy Yorick --enemy Akali --queue 440        # qual partida tinha esses campeões
./lol events <gameId> --type kill -p chogath --from 15:00 --to 16:00
./lol at <gameId> 15:31 --ref 12006,1562                   # quem estava perto de um ponto naquele instante
./lol series <gameId> team --no-header | sort -t$'\t' -k2 -n | head -1   # pior minuto do time
./lol list --queue 440 --ids --n 5 | ./lol fetch - | ./lol summarize --quiet -
```

### Relatório com replay

O jeito mais rápido é começar pelo esqueleto:

```bash
./lol new latest-flex --tema review     # baixa, resume e grava reports/<data>-<id>-review.json com TODOs
./lol swings <gameId>                   # onde o ouro virou
./lol fights <gameId>                   # as lutas
./lol decisions <gameId>                # decisão por decisão: estado na hora, sinais, resultado e chance de vitória
```

Modelos e calibração (precisam de partidas em cache; o crawl usa a Riot API):

```bash
./lol calibrate all --write             # recalibra sinais, pesos dos objetivos e cortes boa/ruim
./lol winprob crawl --max 1000          # baixa ranqueadas do mesmo elo (só features, sem nomes) para data/winprob/
./lol winprob train                     # treina o modelo de chance de vitória
./lol winprob curve <gameId>            # chance de vitória minuto a minuto
./lol junglers --champ Shaco            # o que esse campeão costuma fazer nos primeiros minutos
./lol dashboard --ranked                # reports/painel.html: evolução com metas
./lol pregame --wait                    # na seleção de campeões: lado para começar e o que esperar do jungle inimigo
./lol pregame --ally "Ekko,Cho'Gath,Vladimir,Nilah,Ashe" --enemy "Shaco,Akali,Yorick,Tristana,Thresh" --side blue
```

Depois de trocar os `TODO` do spec pelo seu julgamento, rode `./lol render reports/<arquivo>.json`. O render recusa spec com `TODO`; use `--draft` só para pré-visualizar. Os passos abaixo mostram como montar o spec à mão.


1. Escreva o spec da análise em `reports/<data>-<gameId>-<tema>.json`. O comando `./lol render --example` mostra todos os campos.
2. Rode `./lol render reports/<arquivo>.json`. Ele gera o `.md` e o `.html` ao lado do spec.
3. Abra o `.html` no navegador ou publique como Claude Artifact.

Com o Claude Code, basta pedir em linguagem natural, por exemplo: *"analisa minha última flex, o jungle inimigo gankou o top 3 vezes?"*.

## Testes e contribuição

- `./lol selftest --fixture` roda tudo com 3 partidas sintéticas geradas por `tools/fixture.py`, sem dados pessoais nem cliente aberto. É o que o CI roda (`.github/workflows/selftest.yml`, Python 3.10 e 3.12) a cada push e PR.
- `./lol selftest` sem opção testa com as suas partidas em cache.
- Para explorar a fixture: `./lol fixture /tmp/fx` e depois `LOL_DATA_DIR=/tmp/fx ./lol players 9100000003`.
- Mudou o roteiro de `tools/fixture.py`? Atualize os números conferidos em `test_fixture` no selftest.
- `./lol watch` fica de olho no cliente: quando termina uma partida, baixa, resume e cria o esqueleto do relatório.

## Minimapa

O replay usa como fundo o minimapa oficial do Summoner's Rift, que **não está no repositório** porque é arte da Riot Games. Para usar:

1. Abra no navegador a imagem `Summoner's_Rift_Minimap.png` da [League of Legends Wiki](https://wiki.leagueoflegends.com/en-us/File:Summoner%27s_Rift_Minimap.png).
2. Salve o arquivo original (512×512) como `tools/templates/assets/sr_minimap_512.png`.

Sem esse arquivo, o render desenha um mapa vetorial no lugar.

Os retratos dos campeões são baixados do **seu** cliente pelo `./lol icons` (o `fetch` já faz isso) e ficam em `data/static/champion-icons/`.

## Privacidade

- `data/` (partidas, PUUIDs, nicks) e `reports/` (suas análises) estão no `.gitignore`.
- A senha do lockfile e a `RIOT_API_KEY` nunca são impressas nem gravadas. A API local é usada só para leitura (GET).
- Preferências pessoais para o Claude (sua conta, apelidos de amigos) vão em `CLAUDE.local.md`, que também fica fora do git.

## Limitações

Os dados da timeline registram a posição de cada jogador **uma vez por minuto**. Ficam de fora: ganks sem kill, Flash e outros feitiços, HP, pings e voz. Pela API local também não há wards nem compras por evento. Cada relatório declara isso. Posições entre os minutos, recalls e roles são **estimados** e aparecem marcados como tal.

## Licença

MIT. Veja [LICENSE](LICENSE).

lol-coach isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
