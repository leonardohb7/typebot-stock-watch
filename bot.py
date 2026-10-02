#!/usr/bin/env python3
"""
flow-watch — avisa quando o catalogo de um fluxo Typebot muda.

Percorre um fluxo de conversa Typebot como se fosse um navegador, junta o
texto das telas num relatorio, e compara com a execucao anterior. Se mudou,
notifica via ntfy.sh.

Nao faz parsing de item/preco de proposito: compara o texto inteiro. Menos
coisa pra quebrar quando o fluxo for alterado do outro lado.

Nada do fluxo esta no codigo. Tudo vem do ambiente (secrets no CI) ou de um
config.ini local:

    WATCH_BASE      https://host.do.typebot
    WATCH_FLOW      nome-do-fluxo (o slug na URL do startChat)
    WATCH_ID        o identificador que o fluxo pede no campo de texto
    WATCH_TOPIC     topico do ntfy.sh onde os avisos chegam
    WATCH_ENTRY     regex da opcao que abre o fluxo
    WATCH_SECTIONS  uma linha por secao, no formato ROTULO=regex
    WATCH_MENU      (opcional) regex que identifica o menu principal

Uso:
    python3 bot.py                # uma verificacao
    python3 bot.py --dry-run      # roda, imprime, nao notifica nem salva
    python3 bot.py --loop         # fica rodando, intervalo sorteado
"""

import argparse
import configparser
import difflib
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = os.environ.get("WATCH_BASE", "").rstrip("/")
TYPEBOT = os.environ.get("WATCH_FLOW", "")
NTFY_BASE = "https://ntfy.sh"

# Cabecalhos identicos aos que o Firefox manda. Alem de evitar bloqueio por
# protecao anti-bot da hospedagem, mantem a requisicao consistente: UA de
# navegador com headers de script destoa mais do que nao mascarar nada.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TIMEOUT = 30
RETRIES = 3

# Intervalo do modo --loop, em minutos. Sorteado a cada ciclo.
INTERVALO_MIN = 7
INTERVALO_MAX = 12

# Blocos do richText que viram quebra de linha no texto plano.
BLOCK_TYPES = {"p", "ul", "ol", "li", "h1", "h2", "h3", "blockquote"}

# Limite de mensagem do ntfy (4096 bytes). Deixo folga.
MAX_MSG = 3500


class FlowError(Exception):
    """O fluxo do Typebot nao esta onde a gente esperava."""


# ---------------------------------------------------------------- HTTP


def post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    base_headers = dict(BROWSER_HEADERS)
    base_headers.update(headers or {})
    # Valor None = remover o header (usado pra tirar o disfarce ao falar com o ntfy).
    base_headers = {k: v for k, v in base_headers.items() if v is not None}

    last_error = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=body, method="POST", headers=base_headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 4xx nao adianta repetir.
            if exc.code < 500:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise FlowError("HTTP %s em %s: %s" % (exc.code, url, detail)) from exc
        except Exception as exc:  # rede, timeout, JSON invalido
            last_error = exc
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)

    raise FlowError("falhou apos %d tentativas em %s: %s" % (RETRIES, url, last_error))


# ------------------------------------------------------- richText -> texto


def flatten(node):
    """Achata a arvore richText do Typebot em texto plano legivel."""
    if isinstance(node, list):
        return "".join(flatten(n) for n in node)
    if not isinstance(node, dict):
        return ""

    children = node.get("children")
    if children is None:
        return node.get("text", "")

    inner = flatten(children)
    kind = node.get("type")

    if kind == "li":
        return "- " + inner.strip() + "\n"
    if kind in BLOCK_TYPES:
        return inner + "\n"
    return inner


def messages_to_text(messages):
    """Junta as bolhas de uma resposta num bloco de texto."""
    parts = []
    for msg in messages:
        content = msg.get("content") or {}
        if content.get("type") == "richText":
            parts.append(flatten(content.get("richText", [])))
        elif "url" in content:
            parts.append("[%s] %s" % (msg.get("type", "midia"), content["url"]))
    return tidy("\n".join(parts))


def tidy(text):
    """Tira espaco no fim das linhas e colapsa linhas em branco repetidas."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


# ------------------------------------------------------------- conversa


class Chat:
    def __init__(self):
        self.last = post_json(
            "%s/api/v1/typebots/%s/startChat" % (BASE, TYPEBOT),
            {"isStreamEnabled": False, "prefilledVariables": {}, "isOnlyRegistering": False},
            headers={"Origin": BASE, "Referer": BASE + "/"},
        )
        self.session_id = self.last.get("sessionId")
        if not self.session_id:
            raise FlowError("startChat nao devolveu sessionId")

    @property
    def items(self):
        payload = self.last.get("input") or {}
        return [i.get("content", "") for i in payload.get("items", [])]

    @property
    def input_type(self):
        return (self.last.get("input") or {}).get("type")

    @property
    def text(self):
        return messages_to_text(self.last.get("messages", []))

    def send(self, text):
        self.last = post_json(
            "%s/api/v1/sessions/%s/continueChat" % (BASE, self.session_id),
            {"message": {"type": "text", "text": text}},
            headers={"Origin": BASE, "Referer": BASE + "/"},
        )
        return self.last

    def pick(self, pattern, required=True):
        """Escolhe a opcao cujo texto casa com o regex. Devolve o texto escolhido."""
        rx = re.compile(pattern, re.I)
        for content in self.items:
            if rx.search(content):
                self.send(content)
                return content
        if required:
            raise FlowError(
                "nenhuma opcao casou com /%s/ — opcoes: %r" % (pattern, self.items)
            )
        return None


# ----------------------------------------------------------------- fluxo

GO_ON = re.compile(r"ciente|entendi|continuar|prosseguir|ok", re.I)


def parse_sections(raw):
    """Le WATCH_SECTIONS. Uma linha por secao: ROTULO=regex[@intervalo_min].

    O intervalo e opcional e diz de quantos em quantos minutos aquela secao
    deve ser visitada. 0 ou ausente = toda rodada.
    """
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, resto = line.partition("=")
        if not sep:
            resto = label
        pattern, arroba, cada = resto.partition("@")
        try:
            cada = int(cada) if arroba else 0
        except ValueError:
            cada = 0
        out.append((label.strip().upper(), pattern.strip(), max(0, cada)))
    return out


def menu_hint(sections):
    """Regex que reconhece o menu principal. Padrao: a uniao das secoes."""
    raw = os.environ.get("WATCH_MENU", "").strip()
    if not raw:
        raw = "|".join("(?:%s)" % p for _, p, _ in sections)
    return re.compile(raw or ".", re.I)


def reach_menu(chat, hint, max_hops=5):
    """Avanca por telas intermediarias ate chegar no menu principal."""
    for _ in range(max_hops):
        if any(hint.search(i) for i in chat.items):
            return
        if chat.pick(GO_ON.pattern, required=False):
            continue
        if len(chat.items) == 1:  # tela de "ok" com nome diferente
            chat.send(chat.items[0])
            continue
        raise FlowError("nao cheguei no menu - opcoes: %r" % (chat.items,))
    raise FlowError("nao cheguei no menu depois de %d telas" % max_hops)


def ate_o_menu(cfg, hint):
    """Abre uma sessao nova e navega ate o menu principal."""
    chat = Chat()
    chat.pick(cfg["entry"])
    if chat.input_type != "text input":
        raise FlowError("esperava campo de texto, veio %r" % (chat.input_type,))
    chat.send(cfg["ident"])
    reach_menu(chat, hint)
    return chat


def collect(cfg, devidas):
    """Visita as secoes devidas.

    O fluxo e um funil de mao unica: depois de entrar numa categoria nao ha
    como voltar ao menu. Por isso cada folha (categoria, composto) precisa de
    uma sessao propria. A primeira folha de cada categoria reaproveita a
    sessao que ja esta aberta.

    Devolve (opcoes do menu, {rotulo da folha: (texto, opcoes)}).
    """
    hint = menu_hint(cfg["sections"])
    menu = None
    capturas = {}

    for label, pattern, _ in devidas:
        chat = ate_o_menu(cfg, hint)
        if menu is None:
            menu = list(chat.items)

        chat.pick(pattern)
        compostos = list(chat.items)

        if not compostos:
            # categoria sem passo de composto: ja e a tela final
            capturas[label] = (chat.text, [])
            continue

        for i, composto in enumerate(compostos):
            atual = chat
            if i:
                atual = ate_o_menu(cfg, hint)
                atual.pick(pattern)
            atual.send(composto)
            capturas["%s / %s" % (label, composto)] = (atual.text, list(atual.items))

    return menu, capturas


def render(menu, secoes):
    """Monta o relatorio inteiro a partir do estado acumulado."""
    out = []
    if menu:
        out.append("== MENU ==")
        out.append("opcoes: " + " | ".join(menu))
        out.append("")
    for rotulo in sorted(secoes):
        dados = secoes[rotulo]
        out.append("== %s ==" % rotulo)
        if dados.get("texto"):
            out.append(dados["texto"])
        if dados.get("opcoes"):
            out.append("opcoes: " + " | ".join(dados["opcoes"]))
        out.append("")
    return tidy("\n".join(out))


# --------------------------------------------------------------- estado


def load_config(path):
    """Le config.ini. Ausente ou malformado nao e erro: caimos no ambiente."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return {}
    if not parser.has_section("watch"):
        return {}
    return {k: v.strip() for k, v in parser["watch"].items()}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            estado = json.load(fh)
    except (OSError, ValueError):
        return {"menu": [], "secoes": {}}
    if not isinstance(estado, dict):
        return {"menu": [], "secoes": {}}
    estado.setdefault("menu", [])
    estado.setdefault("secoes", {})
    return estado


def save_state(path, estado):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "menu": estado.get("menu", []),
                "secoes": estado.get("secoes", {}),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            fh,
            ensure_ascii=False,
            indent=1,
        )
        fh.write("\n")


ITEM = re.compile(r"^\[\s*R\$\s*([\d.,]+)\s*\]\s*[\u2014\u2013-]?\s*(.*)$")


def item(opcao):
    """'[R$ 90] - Forbidden (indoor)' -> (90.0, 'Forbidden (indoor)').

    Opcao que nao parece produto volta como (None, texto cru).
    """
    m = ITEM.match((opcao or "").strip())
    if not m:
        return (None, (opcao or "").strip())
    bruto = m.group(1).replace(".", "").replace(",", ".")
    nome = re.sub(r"\s*\(\s*R\$[^)]*\)\s*$", "", m.group(2)).strip()
    try:
        return (float(bruto), nome)
    except ValueError:
        return (None, nome)


def preco(valor):
    if valor is None:
        return ""
    return "R$ %d" % valor if float(valor).is_integer() else "R$ %.2f" % valor


def linha(opcao):
    p, nome = item(opcao)
    return "%s  %s" % (preco(p), nome) if p is not None else nome


def por_nome(opcoes):
    return {item(o)[1]: (item(o)[0], o) for o in (opcoes or [])}


def resumo(antes, agora):
    """Compara dois {rotulo: [opcoes]}. Devolve (chegou, saiu, mudou_preco)."""
    chegou, saiu, mudou = {}, {}, {}
    for rotulo in sorted(set(antes) | set(agora)):
        a = por_nome(antes.get(rotulo))
        b = por_nome(agora.get(rotulo))
        for nome in sorted(b):
            if nome not in a:
                chegou.setdefault(rotulo, []).append(b[nome][1])
            elif a[nome][0] != b[nome][0]:
                mudou.setdefault(rotulo, []).append((nome, a[nome][0], b[nome][0]))
        for nome in sorted(a):
            if nome not in b:
                saiu.setdefault(rotulo, []).append(a[nome][1])
    return chegou, saiu, mudou


def catalogo(secoes):
    """So os produtos, agrupados por secao e ordenados por preco."""
    out = []
    for rotulo in sorted(secoes):
        itens = secoes[rotulo].get("opcoes") or []
        out.append(rotulo)
        if not itens:
            out.append("  (nada disponivel)")
        for o in sorted(itens, key=lambda x: (item(x)[0] is None, item(x)[0] or 0, item(x)[1])):
            out.append("  " + linha(o))
        out.append("")
    return "\n".join(out).strip()


def titulo_mudanca(chegou, saiu, mudou):
    n = lambda d: sum(len(v) for v in d.values())
    partes = []
    if n(chegou):
        partes.append("%d novo%s" % (n(chegou), "s" if n(chegou) > 1 else ""))
    if n(saiu):
        partes.append("%d saiu" % n(saiu) if n(saiu) == 1 else "%d sairam" % n(saiu))
    if n(mudou):
        partes.append("%d de preco" % n(mudou))
    return ", ".join(partes) if partes else "catalogo mudou"


def corpo_mudanca(chegou, saiu, mudou, secoes):
    blocos = []
    if chegou:
        linhas = ["CHEGOU"]
        for rotulo in sorted(chegou):
            for o in chegou[rotulo]:
                linhas.append("  %s  |  %s" % (linha(o), rotulo))
        blocos.append("\n".join(linhas))
    if saiu:
        linhas = ["SAIU"]
        for rotulo in sorted(saiu):
            for o in saiu[rotulo]:
                linhas.append("  %s  |  %s" % (linha(o), rotulo))
        blocos.append("\n".join(linhas))
    if mudou:
        linhas = ["MUDOU DE PRECO"]
        for rotulo in sorted(mudou):
            for nome, antes_p, agora_p in mudou[rotulo]:
                linhas.append("  %s: %s -> %s  |  %s" % (nome, preco(antes_p), preco(agora_p), rotulo))
        blocos.append("\n".join(linhas))
    blocos.append("CATALOGO ATUAL\n" + catalogo(secoes))
    return "\n\n".join(blocos)


def added_lines(old, new):
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), n=0, lineterm="")
    return [
        ln[1:].strip()
        for ln in diff
        if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()
    ]


def categoria(rotulo):
    """'FLORES / THC' -> 'FLORES'."""
    return rotulo.split(" / ")[0]


def secoes_devidas(sections, secoes, agora):
    """Quais secoes tocam nesta rodada, segundo o intervalo de cada uma."""
    devidas = []
    for label, pattern, cada in sections:
        if cada <= 0:
            devidas.append((label, pattern, cada))
            continue
        vistos = [
            v.get("visto", 0) for k, v in secoes.items() if categoria(k) == label
        ]
        if not vistos or (agora - max(vistos)) >= cada * 60:
            devidas.append((label, pattern, cada))
    return devidas


# ------------------------------------------------------------ notificacao


def notify(topic, title, message, tags):
    """ntfy e outro servico: nao herda o disfarce de navegador."""
    payload = {
        "topic": topic,
        "title": title,
        "message": message[:MAX_MSG],
        "tags": tags,
    }
    post_json(
        NTFY_BASE,
        payload,
        headers={
            "User-Agent": "flow-watch",
            "Origin": None,
            "Referer": None,
            "Sec-Fetch-Dest": None,
            "Sec-Fetch-Mode": None,
            "Sec-Fetch-Site": None,
        },
    )


# ------------------------------------------------------------------ main


def ciclo(cfg, state_path, avisou_quebra):
    """Um ciclo completo. Devolve o novo valor de avisou_quebra."""
    topic = cfg["topic"]
    estado = load_state(state_path)
    secoes = dict(estado.get("secoes") or {})
    agora = time.time()

    devidas = secoes_devidas(cfg["sections"], secoes, agora)
    if not devidas:
        print("%s  nenhuma secao devida" % time.strftime("%H:%M"))
        return avisou_quebra

    try:
        menu, capturas = collect(cfg, devidas)
    except FlowError as exc:
        print("FALHA: %s" % exc, file=sys.stderr)
        # So avisa na primeira falha da sequencia: senao vira spam a cada ciclo.
        if topic and not avisou_quebra:
            notify(
                topic,
                "flow-watch quebrou",
                "O fluxo mudou e o script nao conseguiu navegar.\n\n%s" % exc,
                ["warning"],
            )
        return True

    if avisou_quebra:
        print("voltou a funcionar")
        if topic:
            notify(topic, "flow-watch voltou", "Navegacao normalizada.", ["white_check_mark"])

    anterior = {
        k: (v.get("opcoes") or []) for k, v in (estado.get("secoes") or {}).items()
    }

    # As folhas das categorias visitadas sao substituidas por inteiro: um
    # composto pode ter deixado de existir na receita.
    visitadas = {l for l, _, _ in devidas}
    secoes = {k: v for k, v in secoes.items() if categoria(k) not in visitadas}
    for rotulo, (texto, opcoes) in capturas.items():
        secoes[rotulo] = {"texto": texto, "opcoes": opcoes, "visto": agora}

    novo_menu = menu or estado.get("menu") or []
    atual = {k: (v.get("opcoes") or []) for k, v in secoes.items()}

    # A comparacao olha so os produtos. Mudanca no texto das telas nao
    # notifica: e so moldura, e era o que enchia o aviso de ruido.
    chegou, saiu, mudou = resumo(anterior, atual)

    if anterior and not (chegou or saiu or mudou):
        print("%s  sem mudancas (%s)" % (time.strftime("%H:%M"), ", ".join(sorted(visitadas))))
        save_state(state_path, {"menu": novo_menu, "secoes": secoes})
        return False

    if not anterior:
        title = "flow-watch ativo"
        body = "Primeira leitura do catalogo.\n\n" + catalogo(secoes)
        tags = ["seedling"]
    else:
        title = titulo_mudanca(chegou, saiu, mudou)
        body = corpo_mudanca(chegou, saiu, mudou, secoes)
        tags = ["bell"]

    notify(topic, title, body, tags)
    save_state(state_path, {"menu": novo_menu, "secoes": secoes})
    print("%s  mudanca detectada, notificacao enviada" % time.strftime("%H:%M"))
    return False


def main():
    parser = argparse.ArgumentParser(description="Monitor de catalogo de um fluxo Typebot")
    parser.add_argument(
        "--config", default=os.path.join(HERE, "config.ini"), help="arquivo de configuracao"
    )
    parser.add_argument(
        "--state", default=os.path.join(HERE, "estado.json"), help="arquivo de estado"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="imprime o relatorio, nao notifica nem salva"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="fica rodando, com intervalo sorteado entre cada verificacao",
    )
    args = parser.parse_args()

    # Ambiente ganha do arquivo: e assim que o CI injeta os secrets.
    config = load_config(args.config)

    def opt(env, chave, limpa=None):
        valor = os.environ.get(env) or config.get(chave, "")
        return limpa(valor) if limpa else valor.strip()

    cfg = {
        "ident": opt("WATCH_ID", "id", lambda v: re.sub(r"\s", "", v)),
        "topic": opt("WATCH_TOPIC", "topico"),
        "entry": opt("WATCH_ENTRY", "entrada"),
        "sections": parse_sections(os.environ.get("WATCH_SECTIONS") or config.get("secoes", "")),
    }

    def minutos(chave, padrao):
        try:
            return max(1, int(config.get(chave, padrao)))
        except (TypeError, ValueError):
            return padrao

    lo = minutos("intervalo_min", INTERVALO_MIN)
    hi = minutos("intervalo_max", INTERVALO_MAX)
    if hi < lo:
        lo, hi = hi, lo

    faltando = [
        nome
        for nome, valor in (
            ("WATCH_BASE", BASE),
            ("WATCH_FLOW", TYPEBOT),
            ("WATCH_ID", cfg["ident"]),
            ("WATCH_ENTRY", cfg["entry"]),
            ("WATCH_SECTIONS", cfg["sections"]),
        )
        if not valor
    ]
    if faltando:
        sys.exit("erro: faltando configuracao: %s" % ", ".join(faltando))
    if not cfg["topic"] and not args.dry_run:
        sys.exit("erro: WATCH_TOPIC nao configurado (ou use --dry-run pra so testar).")

    if args.dry_run:
        try:
            menu, capturas = collect(cfg, cfg["sections"])
            print(render(menu, {k: {"texto": t, "opcoes": o} for k, (t, o) in capturas.items()}))
        except FlowError as exc:
            sys.exit("FALHA: %s" % exc)
        return

    if not args.loop:
        quebrou = ciclo(cfg, args.state, False)
        sys.exit(1 if quebrou else 0)

    print("Monitorando. Intervalo sorteado entre %d e %d minutos." % (lo, hi))
    print("Para parar, aperte Ctrl+C.\n")
    avisou_quebra = False
    try:
        while True:
            avisou_quebra = ciclo(cfg, args.state, avisou_quebra)
            espera = random.uniform(lo * 60, hi * 60)
            print("   proxima verificacao em %d min %02d s" % divmod(int(espera), 60)[0:2])
            time.sleep(espera)
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
