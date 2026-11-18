# lol-coach

Coach de League of Legends baseado nos **dados da timeline** das suas partidas, feito para usar com o [Claude Code](https://claude.com/claude-code). Você pergunta ("o jungle abandonou o top?", "onde a vantagem caiu?") e a análise responde com **minuto e dado**, classificando cada conclusão como **suportado**, **refutado** ou **inconclusivo**. O resultado sai como relatório em Markdown e como uma página visual com **replay interativo** da partida, que pode ser publicada como Claude Artifact e compartilhada.

*English: a timeline-based League of Legends coach for Claude Code. Pulls your matches from the local League client, builds per-minute summaries (zones, gold/XP/CS diffs, kills, objectives) and renders evidence-based reports with an interactive match replay. Docs and output are in Portuguese.*

## O que tem

- **Dados da partida** pela API local do cliente do LoL (LCU), só leitura. Também funciona com a API oficial Match-V5 (`RIOT_API_KEY`), que traz wards e compras de itens.
- **Resumo por minuto** dos 10 jogadores: zona do mapa (top, mid, bot, rio, jungle aliada/inimiga, base), ouro, XP, CS e diferenças contra o oponente direto, kills com local e quem estava perto, objetivos, recalls inferidos e limitações declaradas.
- **Consultas encadeáveis** (filosofia Unix, TSV ou JSON Lines): `find`, `frames`, `events`, `at`, `series`, `where`.
- **Relatório** gerado a partir de um "spec" com o julgamento da análise. Os números sempre vêm dos dados.
- **Revisão de decisões** momento a momento (foi para a lane, atravessou o mapa, objetivo, recall, morte): estado na hora, sinais, resultado e veredito, com cortes e pesos **calibrados no seu histórico** (`lol calibrate`) e a **chance de vitória** antes → depois de um modelo treinado em partidas do seu elo (`lol winprob`).
- **Ganks e lutas sem abate** (`lol ganks`, `lol skirmishes`), estimados pelo dano a campeões em cada minuto, e **totais da partida** (`lol totals`: pings, Flash, habilidades, estatísticas da Riot), pela API oficial.
- **Visão antes dos objetivos** (`lol vision`), **padrão do jungle inimigo** por campeão (`lol junglers`) e **painel de evolução** com metas (`lol dashboard`).
- **Página visual**: resumo em cards, gráfico de ouro, vereditos, replay com retratos dos campeões no minimapa, torres caindo no horário real, waves de minions (simulação, com checkbox), linha do tempo clicável, mapas das lutas e timing de objetivos.
- **Pré-jogo** (`lol pregame`): na seleção ou no carregamento, diz em que lado estar de jungle por volta de 3:15 (e por onde começar o clear para chegar lá) e o que esperar do jungle inimigo, com uma página curta para ler no celular. No carregamento faz o **scout dos jogadores** pelo cliente: o começo de jogo do jungler inimigo nas últimas partidas dele, além de elo, maestria e alertas de cada um (primeira vez com o campeão, fora da role, sequência de derrotas).
- **Base de partidas do elo** já incluída (`dataset/winprob/`, anonimizada) e uma **rotina noturna** que a amplia e retreina o modelo sozinha.

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
./lol winprob nightly --log data/winprob/nightly.log   # rotina noturna: suas partidas + crawl + padrões + retreino (volta ao modelo anterior se piorar)
./lol winprob export                    # atualiza a base anonimizada versionada em dataset/winprob/
./lol winprob curve <gameId>            # chance de vitória minuto a minuto
./lol junglers --champ Shaco            # o que esse campeão costuma fazer nos primeiros minutos
./lol dashboard --ranked                # reports/painel.html: evolução com metas
```

Depois de trocar os `TODO` do spec pelo seu julgamento (`./lol render --example` mostra todos os campos), rode `./lol render reports/<arquivo>.json`. Ele gera o `.md` e o `.html` ao lado do spec. O render recusa spec com `TODO`; use `--draft` só para pré-visualizar. Abra o `.html` no navegador ou publique como Claude Artifact.

Com o Claude Code, basta pedir em linguagem natural, por exemplo: *"analisa minha última flex, o jungle inimigo gankou o top 3 vezes?"*.

### Pré-jogo e scout

```bash
./lol pregame --html                    # lê o jogo atual (jogo aberto > carregamento > seleção) e grava reports/pregame.html
./lol pregame --listen --scout --html   # espera o carregamento e faz o scout dos jogadores (jungler inimigo primeiro)
./lol pregame --ally "Ekko,Cho'Gath,Vladimir,Nilah,Ashe" --enemy "Shaco,Akali,Yorick,Tristana,Thresh" --side blue
./lol pregame --spectate "Amigo#BR1" --scout --html   # partida em andamento de outra pessoa (espectador da Riot API)
./lol pregame --for Amigo --ally "..." --enemy "..." --me "Ahri"   # pré-jogo de outra pessoa, a partir de um print
```

No Claude Code, o comando **`/pregame`** faz tudo isso com o modelo Haiku, que é rápido. Ele publica o que já dá na seleção, fica ouvindo o cliente e, quando chega o carregamento, republica a página com o scout no mesmo link.

O scout só roda no carregamento ou com o jogo aberto. Na seleção, a Riot esconde os nomes do outro time, e revelar isso é contra as regras dela. Tudo vem da API local do cliente, sem gastar a chave da Riot. Na página e no terminal, os jogadores aparecem pelo campeão, e nenhum nome é gravado. Cada dado vem com o tamanho da amostra (são ~20 partidas por pessoa), e tudo é tendência, não previsão. O pré-jogo nunca escolhe, bane nem mexe em runas.

### Chave da Riot (opcional)

Sem chave, tudo funciona com o cliente aberto (até 20 partidas suas) e com a base incluída no repositório. Com chave, dá para ir além das 20 partidas, ter wards e compras por evento e ampliar a base:

1. Em [developer.riotgames.com](https://developer.riotgames.com), gere a *Development Key* (vale 24 h) ou registre um produto pessoal para ter uma *Personal Key*, que não expira.
2. Salve a chave sozinha em `riot_api_key.txt`, na raiz (o arquivo está no `.gitignore`), ou use a variável `RIOT_API_KEY`.

A Riot criptografa o PUUID por chave. Ao trocar de chave, o `fetch` atualiza o seu PUUID e continua reconhecendo as partidas baixadas com a chave antiga.

### Rotina noturna (Windows)

`./lol winprob nightly` baixa as suas partidas recentes, amplia a base com partidas do seu elo (em largura, a partir dos jogadores das suas partidas, `--depth 3`), refaz os padrões de jungle e retreina o modelo. Se o modelo piorar, volta para o anterior. Para agendar todo dia às 03:00 (ou assim que o PC ligar), rode no PowerShell, a partir da pasta do projeto:

```powershell
$root = (Get-Location).Path
$arg = "--headless cmd.exe /c `"`"$root\lol.cmd`" winprob nightly --max 1000 --log `"$root\data\winprob\nightly.log`"`""
Register-ScheduledTask -TaskName "lol-coach crawl noturno" -Action (New-ScheduledTaskAction -Execute conhost.exe -Argument $arg -WorkingDirectory $root) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 03:00) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3))
```

Com o limite da chave pessoal, são ~1.250 partidas por hora. A Riot não permite usar várias chaves para somar limite.

## Testes e contribuição

- `./lol selftest --fixture` roda tudo com 3 partidas sintéticas geradas por `tools/fixture.py` e com a base de `dataset/`, sem dados pessoais nem cliente aberto. É o que o CI roda (`.github/workflows/selftest.yml`, Python 3.10 e 3.12) a cada push e PR. A `main` exige os dois checks antes do merge.
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

## Base de partidas (já vem no repositório)

`dataset/winprob/` traz as partidas ranqueadas do BR1 que o `lol winprob crawl` já coletou (veja `meta.json`), além do modelo de chance de vitória e dos padrões de jungle por campeão. Na primeira vez que o `winprob` roda sem amostras em `data/winprob/`, a base é copiada para lá sozinha. A partir daí, o pré-jogo, a chance de vitória e os padrões de jungle funcionam sem precisar de chave da Riot. Com uma chave, o `crawl` continua de onde a base parou, sem repetir partidas.

A base é anônima: cada partida tem só números por minuto (ouro, XP, CS, objetivos, posição dos junglers) e os campeões. Não há nomes, Riot IDs, PUUIDs nem o id da partida, que foi trocado por um hash. `LOL_NO_DATASET=1` desliga a cópia automática.

## Privacidade

- `data/` (partidas, PUUIDs, nicks) e `reports/` (suas análises) estão no `.gitignore`. A base pública em `dataset/` é anonimizada (veja acima).
- A senha do lockfile e a `RIOT_API_KEY` nunca são impressas nem gravadas. A API local é usada só para leitura (GET).
- Preferências pessoais para o Claude (sua conta, apelidos de amigos) vão em `CLAUDE.local.md`, que também fica fora do git.

## Limitações

Os dados da timeline registram a posição de cada jogador **uma vez por minuto**. Pela API oficial, entram também a vida, a mana e o dano a campeões em cada minuto, e daí saem os **ganks e lutas sem abate**, marcados como estimados (um gank em que ninguém causou dano continua invisível). Flash, habilidades e pings vêm só como **total da partida**, sem horário; calls e voz ficam de fora. Pela API local não há nada disso, nem wards ou compras por evento. Cada relatório declara isso. Posições entre os minutos, recalls e roles são **estimados** e aparecem marcados como tal.

## Licença

MIT. Veja [LICENSE](LICENSE).

lol-coach isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
