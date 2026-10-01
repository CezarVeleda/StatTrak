# StatTrak Arduino — CS2 Kill Counter

Decoração ativa de setup: um Arduino Uno + LCD 1602 mostrando o total de kills
da conta de CS2, atualizado em tempo real durante a partida. Hoje é um
protótipo (trabalho da cadeira de Sistemas Embarcados); a pasta `Comercial/`
está reservada pra quando pensarmos em vender isso pronto.

Pasta do protótipo: `Prototipo/` (`total_kills.py`, `test_total_kills.py`,
`sketch_sep30a/`, `chave.txt`, `state.json`, `.gitignore`).

## 1. O que já existe

### Hardware / firmware (não mexido)
- Arduino Uno + LCD 1602 (`LiquidCrystal`), ligado via serial USB, 9600 baud.
- `sketch_sep30a.ino`: le uma linha de texto terminada em `\n`, converte pra
  numero e mostra na linha de baixo do LCD, limpando a linha inteira antes de
  cada escrita. Contrato simples e estável — não precisou ser alterado em
  nenhum momento.

### Software (`total_kills.py`)
Problema original: a API da Steam (`ISteamUserStats/GetUserStatsForGame`,
stat `total_kills`) só reflete o total depois que a partida termina e a Steam
sincroniza — minutos de atraso. Um script que só consulta a Steam a cada 60s
deixava o LCD "parado" durante toda a partida.

Solução: modo híbrido usando o **Game State Integration (GSI)** do próprio
CS2, que manda o estado da partida (incluindo kills ao vivo) via POST HTTP
pra um endereço local configurável.

Arquitetura:
- **Servidor HTTP** (`http.server.ThreadingHTTPServer`) rodando numa thread
  separada, escutando `127.0.0.1:3000`, recebendo os POSTs do GSI.
- **Loop de consulta a Steam** na thread principal, a cada 60s, como no
  script original.
- Um `threading.Lock` protege o estado compartilhado entre as duas threads
  (classe `Contexto`).
- `display = total_steam + pendentes`, onde `pendentes` é quanto o GSI já
  viu de kill que a Steam ainda não confirmou.
  - A cada kill via GSI: `pendentes += delta de kills` (ou reseta se for
    partida nova, kills caiu).
  - Quando a Steam sincroniza: `pendentes -= quanto a Steam subiu` (nunca
    soma por cima — por isso não há dupla contagem; validado com teste
    automatizado e também ao vivo).
  - `MODOS_CONTADOS` (hoje só `"competitive"`) filtra quais modos entram em
    `pendentes`. Cada payload de GSI loga o `map.mode` observado, pra dar
    pra ajustar essa constante quando outros modos (ex. Premier) aparecerem
    com nomes diferentes.
- **Timeout de segurança**: se a partida terminar (`map.mode` passa de
  preenchido pra `None`, segundo o próprio GSI) e a Steam não sincronizar em
  30 minutos, os pendentes são zerados (cobre modos que a Steam nunca conta).
  - Essa lógica já passou por um bug real: a primeira versão usava um
    relógio que começava a contar na inicialização do script, não no fim da
    partida — então qualquer partida real com mais de 45 min disparava o
    timeout **no meio do jogo** e zerava kills de verdade (aconteceu ao
    vivo no primeiro teste: 21 pendentes foram zerados errado às 21:36).
    Corrigido pra usar a transição real de `map.mode` como gatilho.
  - A janela em si também foi calibrada ao vivo: comecei com 10 minutos
    (medindo a partir do fim real da partida, já com a correção acima) e,
    num teste real, a Steam demorou mais que isso pra sincronizar — os
    pendentes zeraram de novo (sem bug, só estimativa curta). Subida pra
    **30 minutos**. Mesmo quando zera "cedo demais", o estrago é cosmético:
    quando a Steam sincronizar, ela manda o total final direto, que
    sobrescreve o zeramento sem depender de `pendentes` — o LCD só fica
    "atrasado" por mais alguns minutos, nunca errado pra sempre.
- **Persistência**: `state.json` grava `total_steam`, `pendentes`,
  `ultimo_kills_partida` e o instante da última kill, de forma atômica
  (escreve num `.tmp` e substitui — não corrompe o arquivo se a luz cair no
  meio da escrita). Salvo a cada evento (kill ou heartbeat de 60s), então o
  máximo que se perde num apagão é o intervalo entre o último evento e o
  corte de energia.
- **Resiliência a rede**: se a consulta à Steam falhar, o script não
  quebra — só loga e tenta de novo no próximo ciclo. O GSI roda 100% local,
  então kills continuam sendo contados em tempo real mesmo sem internet. O
  timeout de 10 min só é avaliado quando uma consulta à Steam *tem sucesso*,
  então uma queda de internet sozinha nunca zera pendentes por engano.
- **Segurança**: chave da API em `chave.txt` (fora do controle de versão via
  `.gitignore`, junto com `state.json`); a URL completa da Steam (que contém
  a chave) nunca é logada, nem em erro.
- **Modo `--simular`**: dispara POSTs de GSI falsos pro próprio servidor
  (sem precisar abrir o jogo nem ter o Arduino conectado) pra testar a
  lógica de pendentes rapidamente.
- **Testes automatizados**: `test_total_kills.py`, 17 testes (`unittest`,
  sem dependência externa) cobrindo soma de kills, partida nova, redução ao
  sincronizar com a Steam (sem dupla contagem), o timeout baseado em
  `map.mode` (incluindo um teste de regressão especifico pro bug dos 45
  min: simula 50 min de partida ativa e garante que não zera), e
  persistência em disco.

## 2. Testado até agora

- ✅ `--simular`: sequência de kills + partida nova, valores corretos.
- ✅ 17/17 testes automatizados passando.
- ✅ Partida real (competitive): kills contados em tempo real via GSI, LCD
  acompanhando (`pendentes` subindo de 1 até 21 ao longo da partida).
- ✅ `map.mode=competitive` confirmado como valor real reportado pelo GSI
  nesse modo.
- ⚠️ Glitch de LCD já apareceu em três ocasiões num teste real:
  1. Número corrompido numa única escrita (`2927000000000`) — se
     autocorrigiu sozinho no próximo envio.
  2. O mesmo tipo de corrupção, de novo numa escrita isolada.
  3. Mais grave: a linha "KILLS" (escrita só uma vez no boot, nunca mais
     tocada pelo código) também corrompeu — virou quadrados/`?`/`/`
     mudando pela tela, confirmando que não é a serial, é o próprio LCD.
  Em todas as vezes o log confirmou que o Python mandou só escritas limpas
  (nunca duplicada/concatenada) — não é bug de lógica.
  - **Teste decisivo**: no caso 3, desconectar e reconectar o USB (sem
    mexer em nenhum fio) resolveu completamente — tela voltou limpa. Isso
    aponta pra um glitch recuperável via reset (power-cycle reinicializa o
    LCD do zero no `lcd.begin()`), não necessariamente um fio solto — mas
    como ainda não sabemos a causa raiz, não dá pra descartar fiação
    marginal (ex. um jumper quase caindo) que só "trava" depois de um
    reset. **Solução rápida se acontecer nessa versão de protótipo**:
    desconectar/reconectar o USB do Arduino.
  - Pra versão comercial, isso não pode depender do usuário final
    perceber e replugar manualmente — vale considerar: reset por hardware
    automático (watchdog), conexões soldadas em vez de jumper/protoboard
    (elimina a causa mais provável de contato intermitente), e/ou um
    reset periódico preventivo do LCD pelo firmware.
- ✅ Bug do timeout de 45 min encontrado e corrigido ao vivo durante o
  próprio teste (zerou pendentes reais no meio de uma partida); corrigido,
  coberto por teste de regressão, e os 21 pendentes perdidos foram
  restaurados manualmente no `state.json`.
- ⏳ **Em andamento**: aguardando a Steam sincronizar o total real dessa
  partida de teste, pra confirmar ao vivo que `pendentes` zera sozinho sem
  dupla contagem (matemática já validada por teste automatizado, falta
  confirmação ao vivo).

## 3. Testes manuais ainda pendentes

- Jogar **Premier** (ou outro modo) só pra registrar no log o `map.mode`
  exato reportado, e então decidir se/como adicionar em `MODOS_CONTADOS`.
- Reiniciar o script de propósito **no meio de uma partida real** (hoje só
  coberto por teste automatizado, não vivenciado ao vivo).
- Confirmar que o `.cfg` do GSI sobrevive a uma atualização do CS2 (Valve
  às vezes reseta arquivos de usuário em `game/csgo/cfg` em updates
  grandes — se sumir, só recriar o arquivo).

## 4. Buracos de robustez conhecidos (não corrigidos ainda, documentados)

- Se a porta 3000 já estiver em uso (ex. rodar o script duas vezes), o
  servidor GSI quebra com traceback cru em vez de mensagem amigável.
- Se `chave.txt` não existir, mesma coisa: traceback cru em vez de aviso
  claro apontando o caminho esperado.
- Nenhum dos dois corrompe dados — só não é uma experiência "plug and
  play" ainda.

## 5. Roadmap para virar produto comercial

Nada disso é urgente pro trabalho da faculdade — é o que precisaria mudar
pra vender como produto:

- **Chave de API e SteamID por cliente**: hoje são fixos no código (chave
  pessoal do Cezar, SteamID hardcoded). Pra vender, cada cliente precisa da
  própria chave/conta, o que exige ou (a) um assistente de configuração
  pedindo pro cliente gerar a própria chave da Steam, ou (b) um backend
  próprio fazendo a consulta com uma chave da empresa (mais infra, mais
  custo, e atenção aos termos de uso da Steam Web API).
- **Distribuição**: hoje é um script Python que precisa de Python instalado
  e ser rodado manualmente no terminal. Pra um produto, isso vira um
  executável standalone (ex. PyInstaller) ou, melhor ainda, a lógica migra
  pra dentro do próprio microcontrolador (ex. trocar o Uno por um ESP32 com
  WiFi, eliminando a dependência de um PC ligado rodando o script —
  provavelmente o caminho certo pra um produto de decoração de verdade, mas
  é um redesenho de hardware maior).
- **Auto-start**: hoje não inicia sozinho com o Windows; precisa decisão
  (serviço, tarefa agendada, ou eliminar a dependência do PC como no item
  acima).
- **Setup amigável**: detectar a porta COM do Arduino e o caminho de
  instalação do CS2 automaticamente, em vez de assumir valores padrão;
  mensagens de erro amigáveis em vez de tracebacks (ver seção 4).
- **Escala**: se vários clientes passarem a consultar a API da Steam
  regularmente, vale olhar limites de taxa e termos de uso da Valve.
- **Produto físico**: case/gabinete pro Arduino+LCD — fora do escopo do
  software, mas é um workstream paralelo a planejar.
