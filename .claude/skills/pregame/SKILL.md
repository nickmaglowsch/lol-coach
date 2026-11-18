---
description: Pré-jogo rápido do lol-coach (lado para começar de jungle, o que esperar do jungle inimigo e scout dos jogadores), lido do jogo atual no cliente e publicado no link fixo do pré-jogo. Use na seleção de campeões ou no carregamento.
argument-hint: "[--scout-budget 45]"
model: haiku
disable-model-invocation: true
allowed-tools: Bash(./lol pregame *) Bash(./lol pregame) Artifact
---

# /pregame

Objetivo: responder rápido e atualizar a página assim que houver dado melhor. Não investigue, não leia código, não rode outros comandos além dos dois abaixo.

O link fixo do pré-jogo está em `CLAUDE.local.md` (linha "Pré-jogo"). Para publicar:
- **na primeira publicação desta sessão**: `Artifact` com `action: "read"` e `url` = o link fixo; depois `Artifact` (publish) com `file_path` = `reports/pregame.html` e o mesmo `url`, sem `icon`;
- **nas seguintes**: só o publish (mesmo `file_path` e `url`).
- Se não achar o link no `CLAUDE.local.md`, publique sem `url`, com `icon: "map"`, e avise que o link mudou.

## Passo 1: o que já dá para ler agora
Rode `./lol pregame --html`.
- Funcionou: publique e responda em até 3 linhas (lado recomendado da linha `>>`, confiança, jungle inimigo, link). Se a saída diz `fonte: seleção de campeões`, avise que a página vai melhorar no carregamento e siga para o passo 2. Se a fonte já é carregamento ou jogo aberto, pule direto para o passo 2 mesmo assim (é ele que traz o scout).
- Erro "cliente indisponível" ou lockfile: responda em uma linha e pare.
- Outro erro (fora da seleção, por exemplo na fila): siga para o passo 2.

## Passo 2: esperar o carregamento e fazer o scout
Rode `./lol pregame --listen --scout --html --timeout 570 $ARGUMENTS` com **timeout da ferramenta Bash de 600000 ms**. O comando fica ouvindo o cliente e só termina quando o carregamento tiver os dois times; aí faz o scout (até ~45 s) e grava a página.
- Publique de novo e responda em até 5 linhas, só com o que o script imprimiu:
  - lado recomendado e campos (`>>`) e a confiança;
  - o jungle inimigo: o padrão **deste jogador** (linha "este jogador, últimas N de jungle") se houver, senão o do campeão;
  - até 2 alertas do scout (linhas com `ALERTA`), priorizando os do time inimigo;
  - a lane mais fraca do seu time, se bem negativa;
  - o link.
- "Tempo esgotado esperando o carregamento": diga em uma linha e pare.

Diga que é tendência estimada, não certeza. Não invente nada que não esteja na saída do script. Nunca escreva o Riot ID de ninguém na resposta: só campeões.

Pré-jogo de um amigo ou a partir de um print: este comando não faz. Peça para usar uma sessão normal (de preferência com Sonnet ou Opus), porque reconhecer 10 retratos de campeão no print precisa de mais cuidado.
