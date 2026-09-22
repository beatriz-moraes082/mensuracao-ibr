"""
Rotina de tags do IBR — reclassifica as tags do funil SDR a partir do que
realmente aconteceu, em vez de depender de gatilho do Kommo.

Quatro tags, todas escritas por aqui:

  bot-não-iniciado  o lead nunca respondeu nada
  bot-incompleto    respondeu alguma coisa e não chegou ao fim
  bot-concluído     respondeu até a última pergunta (campo Idade preenchido)
  Interagiu         o SDR mandou mensagem e o lead respondeu DEPOIS dela

As três primeiras são mutuamente exclusivas: aplicar uma remove as outras duas.
A `Interagiu` é independente e convive com qualquer uma delas.

Por que rotina e não automação nativa do Kommo:
  - o gatilho do Kommo não sabe QUEM mandou a mensagem anterior, então não
    consegue separar "respondeu ao consultor" de "respondeu ao bot";
  - o estado do bot só é conhecível depois — metade dos leads responde mais de
    5 minutos após o cadastro, e o salesbot já encerrou a sessão a essa altura.

Uso:
    python3 rotina_tags_ibr.py            # dry-run, não escreve nada
    python3 rotina_tags_ibr.py --executar
    python3 rotina_tags_ibr.py --dias 60 --executar
"""

import os, sys, json, time, collections, datetime
import urllib.parse, urllib.request
from pathlib import Path

# ── configuração ─────────────────────────────────────────────────────────────
PIPELINE_SDR = 13996659

TAG_NAO_INICIADO = 117946
TAG_INCOMPLETO   = 48682
TAG_CONCLUIDO    = 71598
TAG_INTERAGIU    = 88625          # resolvida na partida, este é o fallback
NOMES = {TAG_NAO_INICIADO: "bot-não-iniciado", TAG_INCOMPLETO: "bot-incompleto",
         TAG_CONCLUIDO: "bot-concluído"}

# Idade é a última pergunta do bot: preenchida = concluiu.
IDADE_LEAD, IDADE_CONTATO = 1115072, 1138192
CAMPOS_LEAD    = [1108406, 1108430, 1108460, 1108476, 1115068, 1115070, 1115072]
CAMPOS_CONTATO = [1138172, 1138174, 1138176, 1138178, 1138188, 1138190, 1138192]

# Leads importados do RD nunca passaram pelo bot — ficam de fora.
PREFIXOS_IMPORTACAO = ("leads rd ibr", "importacao_rd", "importar_", "importação rd")

DIAS = 30
EXECUTAR = "--executar" in sys.argv
for i, a in enumerate(sys.argv):
    if a == "--dias" and i + 1 < len(sys.argv):
        DIAS = int(sys.argv[i + 1])


def _env():
    p = Path(__file__).resolve().parent / ".env"
    if p.exists():
        for linha in p.read_text().splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_env()
SUB = os.environ["KOMMO_SUBDOMAIN"].strip()
TOK = os.environ["KOMMO_TOKEN"].strip()
BASE = f"https://{SUB}.kommo.com/api/v4"


# ── API ──────────────────────────────────────────────────────────────────────
def _req(url, metodo="GET", corpo=None, tentativas=4):
    for i in range(tentativas):
        r = urllib.request.Request(
            url, method=metodo,
            data=json.dumps(corpo, ensure_ascii=False).encode() if corpo else None,
            headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return None if resp.status == 204 else json.load(resp)
        except Exception as e:
            if getattr(e, "code", None) == 204:
                return None
            if i == tentativas - 1:
                raise
            time.sleep(1.5 * (i + 1))


def get(path, params=None):
    return _req(BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else ""))


def paginar(path, params, chave, limite=250):
    fora, pagina = [], 1
    while True:
        p = dict(params)
        p.update({"limit": limite, "page": pagina})
        d = get(path, p)
        if not d:
            break
        itens = d.get("_embedded", {}).get(chave, [])
        fora.extend(itens)
        if len(itens) < limite:
            break
        pagina += 1
        if pagina > 400:
            break
    return fora


def eventos_por_dia(tipo, inicio, fim, entidade=None):
    """Eventos de chat só voltam se pedidos por intervalo de dia — nunca por entity_id."""
    fora, d = [], inicio
    while d < fim:
        prox = d + datetime.timedelta(days=1)
        p = {"filter[type]": tipo,
             "filter[created_at][from]": int(d.timestamp()),
             "filter[created_at][to]": int(prox.timestamp()) - 1}
        if entidade:
            p["filter[entity]"] = entidade
        fora.extend(paginar("/events", p, "events"))
        d = prox
    return fora


def id_da_tag_interagiu():
    d = get("/leads/tags", {"limit": 250}) or {}
    for t in d.get("_embedded", {}).get("tags", []):
        if t["name"].strip().lower() == "interagiu":
            return t["id"]
    return TAG_INTERAGIU


# ── coleta ───────────────────────────────────────────────────────────────────
agora = datetime.datetime.now()
inicio = (agora - datetime.timedelta(days=DIAS)).replace(hour=0, minute=0, second=0, microsecond=0)
print(f"[{agora:%d/%m %H:%M}] janela: {inicio:%d/%m} → hoje ({DIAS} dias)", flush=True)

TAG_INTERAGIU = id_da_tag_interagiu()
TAGS_ESTADO = {TAG_NAO_INICIADO, TAG_INCOMPLETO, TAG_CONCLUIDO}

leads = paginar("/leads", {"filter[pipeline_id]": PIPELINE_SDR,
                           "filter[created_at][from]": int(inicio.timestamp()),
                           "filter[created_at][to]": int(agora.timestamp()),
                           "with": "contacts"}, "leads")

escopo, fora_importacao = [], 0
for l in leads:
    emb = l.get("_embedded", {})
    nomes_tags = [t["name"].lower() for t in emb.get("tags", [])]
    if any(any(n.startswith(p) or p in n for p in PREFIXOS_IMPORTACAO) for n in nomes_tags):
        fora_importacao += 1
        continue
    escopo.append(l)
print(f"  leads: {len(escopo)} no escopo ({fora_importacao} de importação RD, fora)", flush=True)

ids_contatos = sorted({c["id"] for l in escopo for c in l.get("_embedded", {}).get("contacts", [])})
campos_contato = {}
for i in range(0, len(ids_contatos), 50):
    p = {"limit": 50}
    for j, cid in enumerate(ids_contatos[i:i + 50]):
        p[f"filter[id][{j}]"] = cid
    d = get("/contacts", p) or {}
    for c in d.get("_embedded", {}).get("contacts", []):
        campos_contato[c["id"]] = {f["field_id"]: f.get("values") or []
                                   for f in (c.get("custom_fields_values") or [])}
print(f"  contatos: {len(campos_contato)}", flush=True)

entrada = eventos_por_dia("incoming_chat_message", inicio, agora + datetime.timedelta(days=1))
saida   = eventos_por_dia("outgoing_chat_message", inicio, agora + datetime.timedelta(days=1))
print(f"  mensagens: {len(entrada)} recebidas, {len(saida)} enviadas", flush=True)


# ── cruzamento ───────────────────────────────────────────────────────────────
# Um evento de chat cai ora no lead, ora no contato. Indexa os dois lados e
# atribui ao lead mais recente daquele contato que já existia quando a mensagem
# chegou (uma pessoa pode ter vários leads ao longo do tempo).
leads_por_id = {l["id"]: l for l in escopo}
leads_por_contato = collections.defaultdict(list)
for l in escopo:
    for c in l.get("_embedded", {}).get("contacts", []):
        leads_por_contato[c["id"]].append(l)
for v in leads_por_contato.values():
    v.sort(key=lambda x: x["created_at"])


def distribuir(eventos):
    por_lead = collections.defaultdict(list)
    for e in eventos:
        tipo, eid, ts = e.get("entity_type"), e.get("entity_id"), e["created_at"]
        if tipo == "lead" and eid in leads_por_id:
            por_lead[eid].append(e)
        elif tipo == "contact":
            alvo = None
            for l in leads_por_contato.get(eid, []):
                if l["created_at"] <= ts + 60:
                    alvo = l
            if alvo:
                por_lead[alvo["id"]].append(e)
    for v in por_lead.values():
        v.sort(key=lambda e: e["created_at"])
    return por_lead


recebidas = distribuir(entrada)
enviadas = distribuir(saida)


def estado_do_bot(lead):
    cf_lead = {f["field_id"]: f.get("values") or [] for f in (lead.get("custom_fields_values") or [])}
    contatos = [c["id"] for c in lead.get("_embedded", {}).get("contacts", [])]
    cf_cont = campos_contato.get(contatos[0], {}) if contatos else {}

    if cf_lead.get(IDADE_LEAD) or cf_cont.get(IDADE_CONTATO):
        return TAG_CONCLUIDO
    respondeu_campo = (any(cf_lead.get(f) for f in CAMPOS_LEAD)
                       or any(cf_cont.get(f) for f in CAMPOS_CONTATO))
    if recebidas.get(lead["id"]) or respondeu_campo:
        return TAG_INCOMPLETO
    return TAG_NAO_INICIADO


def foi_resgatado(lead):
    """SDR mandou mensagem (created_by != 0 = pessoa, não o bot) e o lead
    respondeu depois dela. É a definição literal da tag Interagiu."""
    humanas = [e["created_at"] for e in enviadas.get(lead["id"], []) if e.get("created_by")]
    if not humanas:
        return False
    primeiro_toque = min(humanas)
    return any(e["created_at"] > primeiro_toque for e in recebidas.get(lead["id"], []))


# ── o que precisa mudar ──────────────────────────────────────────────────────
mudancas, ja_certos = [], 0
resumo = collections.Counter()

for l in escopo:
    atuais = {t["id"] for t in l.get("_embedded", {}).get("tags", [])}
    quero_estado = estado_do_bot(l)
    quero_interagiu = foi_resgatado(l)

    alvo = (atuais - TAGS_ESTADO - {TAG_INTERAGIU}) | {quero_estado}
    if quero_interagiu:
        alvo.add(TAG_INTERAGIU)

    if alvo == atuais:
        ja_certos += 1
        continue

    estado_antes = atuais & TAGS_ESTADO
    if estado_antes != {quero_estado}:
        de = "+".join(sorted(NOMES[t] for t in estado_antes)) or "(nenhuma)"
        resumo[f"{de} → {NOMES[quero_estado]}"] += 1
    if quero_interagiu and TAG_INTERAGIU not in atuais:
        resumo["+ Interagiu"] += 1
    if not quero_interagiu and TAG_INTERAGIU in atuais:
        resumo["- Interagiu (sem resgate humano)"] += 1

    mudancas.append({"id": l["id"], "_embedded": {"tags": [{"id": t} for t in sorted(alvo)]}})

print(f"\n  já corretos: {ja_certos}   |   a alterar: {len(mudancas)}")
for k, v in resumo.most_common():
    print(f"    {v:6d}  {k}")

if not mudancas:
    print("\nnada a fazer.")
    sys.exit(0)

if not EXECUTAR:
    print("\n[DRY-RUN] nada foi escrito. Rode com --executar para aplicar.")
    sys.exit(0)

feitos = 0
for i in range(0, len(mudancas), 50):
    lote = mudancas[i:i + 50]
    _req(BASE + "/leads", "PATCH", lote)
    feitos += len(lote)
    print(f"    gravados {feitos}/{len(mudancas)}", flush=True)
    time.sleep(0.4)
print("pronto.")
