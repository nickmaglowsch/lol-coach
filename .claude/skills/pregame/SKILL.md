---
description: Pré-jogo rápido do lol-coach (lado para começar de jungle e o que esperar do jungle inimigo), lido do jogo atual no cliente e publicado no link fixo do pré-jogo. Use no carregamento ou na seleção de campeões.
argument-hint: "[--source selecao|carregamento|jogo] [--wait]"
model: haiku
disable-model-invocation: true
allowed-tools: Bash(./lol pregame *) Bash(./lol pregame) Artifact
---

# /pregame

Objetivo: responder em segundos. Não investigue, não leia código, não rode outros comandos.

1. Rode, com a ferramenta Bash, a partir da raiz do projeto:
   `./lol pregame --html $ARGUMENTS`
   - O script escolhe a fonte sozinho (jogo aberto > carregamento > seleção de campeões) e grava `reports/pregame.html`.
   - Se falhar (cliente fechado, fora de partida, sem campeões ainda), responda em uma linha o que a mensagem de erro diz e pare. Não tente outra coisa.
2. Publique a página no **link fixo do pré-jogo**, que está em `CLAUDE.local.md` (linha "Pré-jogo"):
   - primeiro `Artifact` com `action: "read"` e `url` = o link fixo (necessário para atualizar um artifact de outra sessão);
   - depois `Artifact` (publish) com `file_path` = `reports/pregame.html` e o mesmo `url`. Sem `icon`.
   - Se não achar o link no `CLAUDE.local.md`, publique sem `url` (cria um novo), com `icon: "map"`, e avise que o link mudou.
3. Responda em português, no máximo 4 linhas, só com o que o script imprimiu:
   - o lado recomendado e os campos (linha que começa com `>>`) e a confiança;
   - o principal sobre o jungle inimigo (lado no minuto 3 e primeiro abate);
   - a lane mais fraca do seu time, se houver uma bem negativa;
   - o link.
   Diga que é tendência estimada, não certeza. Não invente nada que não esteja na saída do script.

Pré-jogo de um amigo ou a partir de um print: este comando não faz. Peça para usar uma sessão normal (de preferência com Sonnet ou Opus), porque reconhecer 10 retratos de campeão no print precisa de mais cuidado.
