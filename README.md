# typebot-stock-watch

Avisa no celular quando o catálogo de um fluxo [Typebot](https://typebot.io)
muda — item novo, item voltando ao estoque, preço diferente.

Só Python 3, sem instalar dependência nenhuma. Roda de graça no GitHub Actions.

## Como funciona

Abre uma sessão no fluxo e navega sozinho pelas opções que você configurar.
Junta o texto das telas, compara com a rodada anterior, notifica via
[ntfy.sh](https://ntfy.sh) se mudou.

**Não interpreta nome nem preço** — compara o texto inteiro. Menos elegante,
muito mais difícil de quebrar: o aviso entrega o texto literal que o fluxo
escreveu, já legível. Em compensação, qualquer alteração dispara notificação,
inclusive um typo corrigido ou emoji trocado.

Nada do fluxo monitorado está no código. Host, nome do fluxo, identificador e
os passos da navegação vêm todos de variáveis de ambiente — no CI, de secrets.
O repositório não guarda nem publica o que foi capturado: o estado da rodada
anterior vive no cache do Actions, fora do git.

## Configuração

| Variável | O que é |
|---|---|
| `WATCH_BASE` | host do Typebot, ex. `https://bot.exemplo.com` |
| `WATCH_FLOW` | slug do fluxo, o que aparece na URL do `startChat` |
| `WATCH_ID` | o valor que o fluxo pede no campo de texto |
| `WATCH_TOPIC` | tópico do ntfy.sh onde os avisos chegam |
| `WATCH_ENTRY` | regex da opção que abre o fluxo |
| `WATCH_SECTIONS` | uma linha por seção, no formato `RÓTULO=regex` |
| `WATCH_MENU` | *(opcional)* regex que reconhece o menu principal |

`WATCH_SECTIONS` define a navegação. A primeira seção precisa estar no menu; as
seguintes só são visitadas se a opção aparecer na tela em que o fluxo parou:

```
CATEGORIA A=regex-a
CATEGORIA B=regex-b
```

## Rodar no GitHub Actions

1. Instale o **ntfy** na Play Store ou App Store. Grátis, sem cadastro.
2. Em **Settings → Secrets and variables → Actions**, crie os secrets da tabela.
3. Na aba **Actions**, habilite e rode `check` uma vez à mão.
4. No ntfy, toque no **+** e assine o tópico que você escolheu.

> Tópicos do ntfy.sh são públicos: quem souber o nome recebe seus avisos.
> Use um nome sorteado, não um nome adivinhável.

O workflow verifica a cada ~10 minutos, com um jitter que espalha os horários.
O cron do Actions é *best-effort* — sob carga o GitHub atrasa a execução.

## Rodar localmente

Copie `config.ini.exemplo` para `config.ini` e preencha. As variáveis de
ambiente, se existirem, têm prioridade sobre o arquivo.

```
python3 bot.py              # uma verificação
python3 bot.py --dry-run    # imprime, não notifica nem salva
python3 bot.py --loop       # fica rodando, intervalo sorteado
```

Apagar o `estado.json` faz a próxima execução mandar o catálogo inteiro de novo
— útil pra reconfirmar que está funcionando sem esperar o estoque mudar.

## Quando quebrar

Vai quebrar quando mexerem no fluxo do outro lado. Chega um aviso
**"flow-watch quebrou"** com as opções que não foram reconhecidas — uma vez por
sequência de falhas, não a cada ciclo. O conserto costuma ser ajustar um regex
em `WATCH_SECTIONS`, sem tocar no código.

---

MIT.
