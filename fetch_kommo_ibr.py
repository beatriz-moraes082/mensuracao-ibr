"""
Busca leads do IBR (Ipioca Beach Residence) no Kommo CRM e salva JSON
estruturado para o dashboard dinâmico.

Saída: data/kommo_leads.json

Espelha o pipeline do IMR (mensuracao-mme/fetch_kommo_imr.py), adaptado para
os funis, status e campos personalizados reais da conta `ipiocabeachresidence`.

⚠️  Regra crítica de venda: status 142 só é VENDA no funil **Closer**
    (e no Base Closer RD / Nutrição). No funil **SDR** e no **Importação RD**
    o 142 chama-se "Reunião realizada"/"Ganho" e NÃO é venda.
"""

import json, os, requests, hashlib
from datetime import datetime, date, timezone
from pathlib import Path

from ibr_normalize import (normalize_canal, normalize_audience_kommo,
                           normalize_creative)


def _load_env():
    """Lê .env (formato KEY=VALUE) da raiz do projeto e popula os.environ."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists(): return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

SUBDOMAIN = os.environ["KOMMO_SUBDOMAIN"].strip()
TOKEN     = os.environ["KOMMO_TOKEN"].strip()

# ── Pipelines IBR (IDs confirmados via /api/v4/leads/pipelines) ──────────────
PIPELINE_SDR       = 13996659   # SDR (funil principal de tráfego pago)
PIPELINE_CLOSER    = 13994627   # Closer (aqui 142 = venda ganha de verdade)
PIPELINE_NUTRICAO  = 13994751   # Nutrição (SDR + Closer)
PIPELINE_RD_SDR    = 13949375   # Importação RD (SDR) — base legada importada
PIPELINE_RD_CLOSER = 14208303   # Base Closer RD — disparo/qualificação da base
PIPELINE_TESTE     = 14250891   # TESTE Thiago — ignorado

# ── Status IBR ───────────────────────────────────────────────────────────────
# SDR (13996659) — 142 aqui = "Reunião realizada" (sucesso do SDR, NÃO é venda)
SDR_ABORDAGEM = 108025395
SDR_PREATEND  = 108025399
SDR_NOVO      = 108025403
SDR_FOLLOWUP  = 108025515
SDR_QUALIFIC  = 108025519   # Qualificação (em processo)
SDR_QUALIF    = 108025523   # Lead qualificado
SDR_REUNIAO   = 108025527   # Reunião agendada
# Closer (13994627) — 142 aqui = venda real
CLO_REALIZ    = 108008379   # Reunião realizada
CLO_PROP      = 108008383   # Proposta enviada
CLO_FOLLOW    = 108008387   # Follow-up
CLO_VERDE     = 108008503   # Sinal verde
CLO_DADOS     = 110383871   # Dados de venda solicitados
CLO_TRAVADOS  = 108904227   # Travados
# Nutrição (13994751)
NUT_SDR       = 108009223
NUT_CLOSER    = 108009231
# Os dois funis da base RD (13949375 e 14208303) não têm IDs fixos aqui:
# as etapas deles já foram renomeadas/repropostas no Kommo, então o dashboard
# os lê pelo status_map coletado a cada rodada.

WON  = 142   # "Venda ganha" nativo — significado varia por pipeline (ver docstring)
LOST = 143   # "Venda perdida" nativo

# ── Campos personalizados do LEAD ────────────────────────────────────────────
CF_SCORE        = 1108478   # Lead score (A/B/C/D/E)
CF_INVESTIMENTO = 1108406   # Investimento em férias
CF_FREQ_VIAGEM  = 1108430   # Frequência de viagens
CF_HOSPEDAGEM   = 1108460   # Tipo de hospedagem
CF_MELHORAR     = 1108476   # Se pudesse melhorar
CF_CLOSER       = 1114558   # Closer responsável
CF_SDR          = 1114596   # SDR responsável
CF_DATA_REUNIAO = 1114604   # Data da reunião realizada
CF_CEP          = 1115068
CF_PROFISSAO    = 1115070
CF_IDADE        = 1115072
# Qualificação do Closer (preenchida na reunião)
CF_CONHECE_MCZ  = 1116698   # Conhece Maceió?
CF_HABITO_VIAJ  = 1116700   # Possui hábito de viajar?
CF_QTD_VIAGENS  = 1116706   # Qtd. de viagens/ano
CF_PERIODO_VIAJ = 1116708   # Período que viaja (multiselect)
CF_FACILITADOR  = 1116710   # Facilitador de viagem (multiselect)
CF_ASSISTIU     = 1116712   # Assistiu apresentação
CF_INTERESSE    = 1116714   # Interesse real?
CF_COND_FIN     = 1116716   # Condição financeira
CF_PROB_FECHAR  = 1116722   # Probabilidade de fechamento
CF_PRODUTO      = 1125656   # Produto (Closer)
CF_PRODUTO_ALT  = 1116732   # Produto (campo legado)
CF_FORMATO_REU  = 1125658   # Formato de reunião
CF_OBJECAO      = 1132548   # Objeção
CF_OBJECAO_RD   = 1131182   # Objeção (base RD)
CF_MOTIVO_PERDA = 1131210   # Motivo da perda (base RD)
CF_OBSERVACAO   = 1132546

# ── Campos personalizados do CONTATO ─────────────────────────────────────────
CF_ORIGEM   = 1123794   # Origem  (Meta+Ads / google / hablla.io / unknown ...)
CF_CAMPANHA = 1123796   # Campanha (slug — às vezes público, às vezes campanha)
CF_ANUNCIO  = 1123798   # Anúncio (criativo)

# Tags (lowercase) que indicam reunião — mesma convenção do IMR
TAGS_REUNIAO = {"reunião-agendada", "reunião-realizada", "reagendar-reunião"}

# Período: desde o início da operação rastreada até hoje.
PERIOD_START = date(2026, 1, 1)
PERIOD_END   = date.today()


# Normalização de canal / público / criativo vive em ibr_normalize.py — o mesmo
# módulo que o fetch_meta_spend.py usa, para que os dois lados do cruzamento
# (lead do Kommo × gasto do Meta) cheguem exatamente ao mesmo rótulo.


def week_of(ts):
    d = datetime.fromtimestamp(ts).date()
    if d.day <= 7:  return "w1"
    if d.day <= 14: return "w2"
    if d.day <= 21: return "w3"
    return "w4"


def month_of(ts):
    d = datetime.fromtimestamp(ts).date()
    return f"{d.year:04d}-{d.month:02d}"


# ═══════════════════════════════════════════════════════════════════════════
#  Kommo API
# ═══════════════════════════════════════════════════════════════════════════

def hdrs():
    return {"Authorization": f"Bearer {TOKEN}"}


def kommo_get(path, params=None):
    r = requests.get(f"https://{SUBDOMAIN}.kommo.com{path}", headers=hdrs(), params=params, timeout=90)
    # 401/403 devolvendo {} faria a coleta parecer "conta vazia" e sair com
    # código 0 — foi assim que a primeira execução agendada publicou zero lead.
    if r.status_code in (401, 403):
        raise SystemExit(
            f"Kommo recusou a credencial ({r.status_code}) em {path}.\n"
            f"Verifique KOMMO_TOKEN: expirado, truncado na cópia ou com espaço/quebra de linha."
        )
    if r.status_code == 429 or r.status_code >= 500:
        raise SystemExit(f"Kommo respondeu {r.status_code} em {path} — coleta abortada.")
    # 204 sem corpo é resposta legítima do Kommo para página vazia.
    return r.json() if r.ok and r.text else {}


def _period_ts():
    ts_from = int(datetime(PERIOD_START.year, PERIOD_START.month, PERIOD_START.day).timestamp())
    ts_to   = int(datetime(PERIOD_END.year, PERIOD_END.month, PERIOD_END.day, 23, 59, 59).timestamp())
    return ts_from, ts_to


def _paged_leads(params):
    leads, page = [], 1
    while True:
        p = dict(params); p.update({"limit": 250, "page": page, "with": "contacts"})
        data = kommo_get("/api/v4/leads", params=p)
        batch = data.get("_embedded", {}).get("leads", [])
        if not batch: break
        leads.extend(batch)
        if len(batch) < 250: break
        page += 1
    return leads


def get_leads(pipeline_id):
    ts_from, ts_to = _period_ts()
    return _paged_leads({
        "filter[pipeline_id]":      pipeline_id,
        "filter[created_at][from]": ts_from,
        "filter[created_at][to]":   ts_to,
    })


def get_leads_closed(pipeline_id):
    """Leads fechados (won/lost) dentro do período — filtro por closed_at.
    Pega vendas de leads criados antes do início do período."""
    ts_from, ts_to = _period_ts()
    return _paged_leads({
        "filter[pipeline_id]":     pipeline_id,
        "filter[closed_at][from]": ts_from,
        "filter[closed_at][to]":   ts_to,
    })


def get_contacts_map(contact_ids):
    result = {}
    ids = list(set(contact_ids))
    total = len(ids)
    for i in range(0, total, 50):
        batch = ids[i:i+50]
        params = {"limit": 250}
        for j, cid in enumerate(batch):
            params[f"filter[id][{j}]"] = cid
        data = kommo_get("/api/v4/contacts", params=params)
        for c in data.get("_embedded", {}).get("contacts", []):
            cf_map = {}
            for cf in (c.get("custom_fields_values") or []):
                vals = cf.get("values") or []
                val = str(vals[0].get("value", "")) if vals else ""
                if not val: continue
                cf_map[cf["field_id"]] = val
                code = cf.get("field_code") or ""
                if code: cf_map[code] = val
            cf_map["_name"] = (c.get("name") or "").strip()
            result[c["id"]] = cf_map
        if (i // 50) % 20 == 0:
            print(f"    contatos {min(i+50,total)}/{total}")
    return result


def get_users_map():
    users, page = {}, 1
    while True:
        data = kommo_get("/api/v4/users", params={"limit": 250, "page": page})
        batch = data.get("_embedded", {}).get("users", []) if data else []
        if not batch: break
        for u in batch:
            # Só o nome: o dash usa apenas isso, e o JSON é publicado em
            # repositório público — não há por que expor o e-mail do time.
            users[u["id"]] = {"name": u.get("name", "")}
        if len(batch) < 250: break
        page += 1
    return users


def get_task_types():
    data = kommo_get("/api/v4/account", params={"with": "task_types"})
    if not data: return {}
    types = data.get("_embedded", {}).get("task_types", []) or []
    return {t["id"]: t.get("name", f"Tipo {t['id']}") for t in types}


def fetch_tasks():
    """Tarefas atreladas a leads — base da aba de Atividades (produtividade do time)."""
    tasks, page = [], 1
    while True:
        data = kommo_get("/api/v4/tasks", params={"limit": 250, "page": page})
        batch = data.get("_embedded", {}).get("tasks", []) if data else []
        if not batch: break
        for t in batch:
            if t.get("entity_type") != "leads": continue
            # Só os campos que a aba de Atividades usa — são 13 mil tarefas, e
            # cada campo extra vira ~200 KB no JSON que o navegador baixa.
            # complete_till e updated_at entram porque sustentam "prazo" e
            # "ritmo" no perfil comportamental: sem eles não dá para saber se a
            # tarefa venceu nem quanto tempo levou para ser concluída.
            tasks.append({
                "responsible":  t.get("responsible_user_id"),
                "task_type_id": t.get("task_type_id"),
                "created_at":   t.get("created_at"),
                "is_completed": bool(t.get("is_completed")),
                "due":          t.get("complete_till"),
                # Aproximação de "quando concluiu": o Kommo não expõe a data de
                # conclusão, e updated_at de tarefa concluída é a última mexida.
                "updated_at":   t.get("updated_at"),
            })
        print(f"    tasks pg{page}: +{len(batch)} (total {len(tasks)})")
        if len(batch) < 250: break
        page += 1
    return tasks


def get_pipeline_statuses():
    status_map = {}
    for pid in [PIPELINE_SDR, PIPELINE_CLOSER, PIPELINE_NUTRICAO,
                PIPELINE_RD_SDR, PIPELINE_RD_CLOSER]:
        data = kommo_get(f"/api/v4/leads/pipelines/{pid}")
        for st in data.get("_embedded", {}).get("statuses", []):
            status_map[st["id"]] = st["name"]
    return status_map


def get_loss_reasons():
    reasons, page = {}, 1
    while True:
        data = kommo_get("/api/v4/leads/loss_reasons", params={"limit": 250, "page": page})
        batch = data.get("_embedded", {}).get("loss_reasons", []) if data else []
        if not batch: break
        for lr in batch: reasons[lr["id"]] = lr.get("name", "")
        if len(batch) < 250: break
        page += 1
    return reasons


# ═══════════════════════════════════════════════════════════════════════════
#  Processamento
# ═══════════════════════════════════════════════════════════════════════════

def lead_contact_id(lead):
    cs = lead.get("_embedded", {}).get("contacts", [])
    return cs[0]["id"] if cs else 0


def get_lead_cf(lead, field_id, field_name=None):
    """Busca por field_id; fallback case-insensitive por nome (resolve ID recriado)."""
    for cf in (lead.get("custom_fields_values") or []):
        if cf["field_id"] == field_id:
            vals = cf.get("values") or []
            return str(vals[0]["value"]) if vals else ""
    if field_name:
        target = field_name.strip().lower()
        for cf in (lead.get("custom_fields_values") or []):
            if (cf.get("field_name") or "").strip().lower() == target:
                vals = cf.get("values") or []
                return str(vals[0]["value"]) if vals else ""
    return ""


def get_lead_cf_multi(lead, field_id, field_name=None):
    for cf in (lead.get("custom_fields_values") or []):
        if cf["field_id"] == field_id:
            return [str(v.get("value", "")) for v in (cf.get("values") or [])]
    if field_name:
        target = field_name.strip().lower()
        for cf in (lead.get("custom_fields_values") or []):
            if (cf.get("field_name") or "").strip().lower() == target:
                return [str(v.get("value", "")) for v in (cf.get("values") or [])]
    return []


def get_lead_tags(lead):
    tags = lead.get("_embedded", {}).get("tags", []) or []
    return [(t.get("name") or "").strip().lower() for t in tags]


def classify(pipeline, status, tags, closed_in_period):
    """Regras de negócio do IBR.

    SDR:
      qualified  = status em {Lead qualificado, Reunião agendada, 142}
                   OU tem tag de reunião (quem agendou passou pela qualificação)
      reuniao_agendada  = tag de reunião
      reuniao_realizada = status 142 (que no SDR se chama "Reunião realizada")
                          OU tag 'reunião-realizada'
    Closer:
      proposta = Proposta enviada + Follow-up + Sinal verde + Dados de venda solicitados
      venda    = status 142 no Closer E fechado dentro do período

    Os funis da base RD ficam de fora da qualificação de propósito: são
    trabalho de disparo sobre base importada, e as etapas deles foram
    renomeadas no Kommo mais de uma vez. O dashboard os lê por nome de etapa
    (status_map), que acompanha qualquer renomeação.
    """
    qualified = reuniao_agendada = reuniao_realizada = proposta = venda = False
    tag_set = set(tags)
    if pipeline == PIPELINE_SDR:
        reuniao_agendada  = bool(tag_set & TAGS_REUNIAO)
        reuniao_realizada = (status == WON) or ("reunião-realizada" in tag_set)
        qualified = (status in {SDR_QUALIF, SDR_REUNIAO, WON}) or reuniao_agendada
    elif pipeline == PIPELINE_CLOSER:
        proposta = status in {CLO_PROP, CLO_FOLLOW, CLO_VERDE, CLO_DADOS}
        venda    = (status == WON) and closed_in_period
    elif pipeline in (PIPELINE_NUTRICAO, PIPELINE_RD_CLOSER):
        # Nesses funis o 142 também significa venda ganha.
        venda = (status == WON) and closed_in_period
    return qualified, reuniao_agendada, reuniao_realizada, proposta, venda


def process_lead(lead, contacts_map):
    cid       = lead_contact_id(lead)
    contact   = contacts_map.get(cid, {})
    status    = lead.get("status_id", 0)
    pipeline  = lead.get("pipeline_id", 0)
    ts        = lead.get("created_at", 0)
    closed_at = lead.get("closed_at", 0) or 0
    ts_from, ts_to = _period_ts()
    closed_in_period = bool(closed_at and ts_from <= closed_at <= ts_to)

    raw_phone = (contact.get("PHONE") or contact.get(918982, "") or "")
    raw_phone = raw_phone.replace("+", "").replace(" ", "").replace("-", "")
    phone_hash = raw_phone[-10:] if raw_phone else ""
    phone_masked = (raw_phone[:2] + "X"*(len(raw_phone)-6) + raw_phone[-4:]) if len(raw_phone) >= 10 else ""
    dkey = hashlib.sha1(phone_hash.encode()).hexdigest()[:12] if len(phone_hash) >= 10 else ""

    raw_email = (contact.get("EMAIL") or "").strip().lower()
    ekey = hashlib.sha1(raw_email.encode()).hexdigest()[:12] if raw_email else ""
    # Email mascarado — o JSON é publicado no GitHub Pages, então não vai
    # e-mail em claro. O ekey preserva dedup/cruzamento futuro (RD Station).
    if raw_email and "@" in raw_email:
        u, d = raw_email.split("@", 1)
        email_masked = (u[:2] + "*"*max(1, len(u)-2)) + "@" + d
    else:
        email_masked = ""

    # Nome: só primeiro nome + inicial do sobrenome (evita PII em repo público).
    full_name = contact.get("_name", "") or (lead.get("name") or "")
    parts = [p for p in full_name.split() if p]
    name_short = (parts[0] + (" " + parts[1][0] + "." if len(parts) > 1 else "")) if parts else ""

    origem_raw = contact.get(CF_ORIGEM, "")
    camp_raw   = contact.get(CF_CAMPANHA, "")
    anun_raw   = contact.get(CF_ANUNCIO, "")

    tags = get_lead_tags(lead)
    qualified, reuniao_agendada, reuniao_realizada, proposta, venda = classify(
        pipeline, status, tags, closed_in_period)

    return {
        "id":          lead["id"],
        "contact_id":  cid,
        "name":        name_short,
        "responsible_user_id": lead.get("responsible_user_id") or 0,
        "created_at":  ts,
        "closed_at":   closed_at,
        "closed_in_period": closed_in_period,
        "week":        week_of(ts) if ts else "w4",
        "month":       month_of(ts) if ts else "2026-01",
        "status":      status,
        "pipeline":    pipeline,
        "loss_reason_id": lead.get("loss_reason_id") or 0,
        "price":       lead.get("price", 0) or 0,
        "score":       get_lead_cf(lead, CF_SCORE, "Lead score"),
        # Atribuição
        "origem":      origem_raw,
        "canal":       normalize_canal(origem_raw),
        "campaign":    camp_raw.replace("+", " ").strip(),
        "audience":    normalize_audience_kommo(camp_raw),
        "creative":    normalize_creative(anun_raw),
        "creative_raw": anun_raw.replace("+", " ").strip(),
        # Identificação (mascarada)
        "phone":       phone_masked,
        "dkey":        dkey,
        "email":       email_masked,
        "ekey":        ekey,
        "_phone_key":  phone_hash,   # só em memória — removido antes de salvar
        "tags":        tags,
        # Bot de pré-atendimento (campos do lead)
        "hospedagem":   get_lead_cf(lead, CF_HOSPEDAGEM,   "Tipo de hospedagem"),
        "freq_viagem":  get_lead_cf(lead, CF_FREQ_VIAGEM,  "Frequência de viagens"),
        "investimento": get_lead_cf(lead, CF_INVESTIMENTO, "Investimento em férias"),
        "melhorar":     get_lead_cf(lead, CF_MELHORAR,     "Se pudesse melhorar"),
        # Perfil declarado
        "idade":       get_lead_cf(lead, CF_IDADE,     "Idade"),
        "profissao":   get_lead_cf(lead, CF_PROFISSAO, "Profissão"),
        "cep":         get_lead_cf(lead, CF_CEP,       "CEP"),
        # Qualificação do Closer (preenchida na reunião)
        "sdr":            get_lead_cf(lead, CF_SDR,          "SDR"),
        "closer":         get_lead_cf(lead, CF_CLOSER,       "Closer"),
        "data_reuniao":   get_lead_cf(lead, CF_DATA_REUNIAO, "Data da Reunião Realizada"),
        "conhece_maceio": get_lead_cf(lead, CF_CONHECE_MCZ,  "Conhece Maceió?"),
        "habito_viajar":  get_lead_cf(lead, CF_HABITO_VIAJ,  "Possui hábito de viajar?"),
        "qtd_viagens":    get_lead_cf(lead, CF_QTD_VIAGENS,  "Qtd. de Viagens/ano?"),
        "periodo_viaja":  ",".join(get_lead_cf_multi(lead, CF_PERIODO_VIAJ, "Período que viaja?")),
        "facilitador":    ",".join(get_lead_cf_multi(lead, CF_FACILITADOR,  "Facilitador Viagem")),
        "assistiu":       get_lead_cf(lead, CF_ASSISTIU,    "Assistiu apresentação"),
        "interesse_real": get_lead_cf(lead, CF_INTERESSE,   "Interesse real?"),
        "cond_financeira":get_lead_cf(lead, CF_COND_FIN,    "Condição financeira?"),
        "prob_fechar":    get_lead_cf(lead, CF_PROB_FECHAR, "Probrabilidade de fechamento?"),
        "formato_reuniao":get_lead_cf(lead, CF_FORMATO_REU, "Formato de Reunião"),
        "produto":        get_lead_cf(lead, CF_PRODUTO, "Produto") or get_lead_cf(lead, CF_PRODUTO_ALT),
        "objecao":        get_lead_cf(lead, CF_OBJECAO, "Objeção") or get_lead_cf(lead, CF_OBJECAO_RD),
        "motivo_perda":   get_lead_cf(lead, CF_MOTIVO_PERDA, "Motivo Perda"),
        # Flags derivadas
        "qualified":         qualified,
        "reuniao_agendada":  reuniao_agendada,
        "reuniao_realizada": reuniao_realizada,
        "proposta":          proposta,
        "venda":             venda,
        "perda":             status == LOST,
    }


# Chaves que ficam no JSON mesmo quando vazias (o frontend agrupa por elas).
_ALWAYS_KEEP = {"id", "created_at", "status", "pipeline", "month", "week", "canal"}


def rd_summary(leads):
    """Resume um funil da base RD em contagens por etapa e por dia.

    São ~21 mil negócios importados que o dashboard só usa para mostrar quantos
    estão em cada etapa. Mandar o lead inteiro custaria ~5 MB de download por
    visita para responder uma pergunta de contagem.
    """
    out = {}
    for l in leads:
        st = str(l["status"])
        day = datetime.fromtimestamp(l["created_at"]).strftime("%Y-%m-%d") if l["created_at"] else "—"
        out.setdefault(st, {})
        out[st][day] = out[st].get(day, 0) + 1
    return out


def slim(lead):
    """Remove campos vazios antes de salvar.

    O JSON é baixado inteiro pelo navegador a cada abertura do dashboard —
    campo vazio repetido milhares de vezes vira megabyte de tráfego sem
    informação nenhuma.
    """
    return {k: v for k, v in lead.items() if v not in ("", 0, False, [], None) or k in _ALWAYS_KEEP}


def main():
    print(f"\n{'='*62}")
    print("  Kommo IBR → data/kommo_leads.json")
    print(f"  Conta: {SUBDOMAIN} · Período: {PERIOD_START} → {PERIOD_END}")
    print(f"{'='*62}\n")

    print("📋 Status dos pipelines...")
    status_map = get_pipeline_statuses()
    print(f"  {len(status_map)} status mapeados")

    print("📋 Motivos de perda...")
    loss_reasons_map = get_loss_reasons()
    print(f"  {len(loss_reasons_map)} motivos")

    print("👥 Usuários (SDRs/Closers)...")
    users_map = get_users_map()
    print(f"  {len(users_map)} usuários")

    print("\n📋 Tipos de tarefa e tarefas...")
    task_types = get_task_types()
    tasks = fetch_tasks()
    print(f"  {len(tasks)} tarefas")

    print("\n📋 Leads SDR...")
    leads_sdr = get_leads(PIPELINE_SDR);            print(f"  {len(leads_sdr)}")
    print("📋 Leads Closer (criados)...")
    leads_closer = get_leads(PIPELINE_CLOSER);      print(f"  {len(leads_closer)}")
    print("📋 Leads Closer (fechados no período)...")
    leads_closer_closed = get_leads_closed(PIPELINE_CLOSER); print(f"  {len(leads_closer_closed)}")

    seen_ids, leads_closer_all = set(), []
    for l in leads_closer + leads_closer_closed:
        if l["id"] in seen_ids: continue
        seen_ids.add(l["id"]); leads_closer_all.append(l)
    print(f"  merge Closer: {len(leads_closer_all)}")

    print("📋 Leads Nutrição...")
    leads_nutricao = get_leads(PIPELINE_NUTRICAO);  print(f"  {len(leads_nutricao)}")
    print("📋 Leads Importação RD (SDR)...")
    leads_rd_sdr = get_leads(PIPELINE_RD_SDR);      print(f"  {len(leads_rd_sdr)}")
    print("📋 Leads Base Closer RD...")
    leads_rd_closer = get_leads(PIPELINE_RD_CLOSER);print(f"  {len(leads_rd_closer)}")

    all_leads = (leads_sdr + leads_closer_all + leads_nutricao +
                 leads_rd_sdr + leads_rd_closer)
    contact_ids = [lead_contact_id(l) for l in all_leads if lead_contact_id(l)]
    print(f"\n👥 Buscando {len(set(contact_ids))} contatos...")
    contacts_map = get_contacts_map(contact_ids)

    processed_sdr       = [process_lead(l, contacts_map) for l in leads_sdr]
    processed_closer    = [process_lead(l, contacts_map) for l in leads_closer_all]
    processed_nutricao  = [process_lead(l, contacts_map) for l in leads_nutricao]
    processed_rd_sdr    = [process_lead(l, contacts_map) for l in leads_rd_sdr]
    processed_rd_closer = [process_lead(l, contacts_map) for l in leads_rd_closer]

    # Dedup do SDR por telefone (mesma pessoa reentrando pelo anúncio)
    seen_phones, deduped_sdr = set(), []
    for l in processed_sdr:
        p = l.get("_phone_key", "")
        if p and len(p) >= 10:
            if p in seen_phones: continue
            seen_phones.add(p)
        deduped_sdr.append(l)

    # Leads de campanha (aba principal): mídia paga no SDR + os movidos p/ Nutrição
    CAMPANHA_CANAIS = {"Meta Ads", "Google Ads"}
    camp_phones, leads_campanha = set(), 0
    for l in deduped_sdr + processed_nutricao:
        if l.get("canal") not in CAMPANHA_CANAIS: continue
        p = l.get("_phone_key", "")
        if p and len(p) >= 10:
            if p in camp_phones: continue
            camp_phones.add(p)
        leads_campanha += 1

    for lst in (deduped_sdr, processed_closer, processed_nutricao,
                processed_rd_sdr, processed_rd_closer):
        for l in lst:
            l.pop("_phone_key", None)

    print(f"\n  SDR bruto: {len(processed_sdr)} → deduplicado: {len(deduped_sdr)}")
    print(f"  Leads de campanha (SDR+Nutrição, únicos): {leads_campanha}")

    from collections import Counter
    print(f"  Scores: {dict(Counter(l['score'] for l in deduped_sdr if l['score']))}")
    print(f"  Canais: {dict(Counter(l['canal'] for l in deduped_sdr).most_common(8))}")
    print(f"  Públicos: {dict(Counter(l['audience'] for l in deduped_sdr if l['audience'] != '—').most_common(8))}")
    print(f"  Criativos: {dict(Counter(l['creative'] for l in deduped_sdr if l['creative'] != '—').most_common(8))}")

    # Dedup de vendas: mesmo contato fechado no mesmo dia = 1 venda
    closer_id_to_contact = {l["id"]: lead_contact_id(l) for l in leads_closer_all}
    seen_venda_keys, venda_dups = set(), 0
    for l in processed_closer:
        if not l["venda"]: continue
        cid = closer_id_to_contact.get(l["id"], 0)
        closed_day = datetime.fromtimestamp(l["closed_at"]).strftime("%Y-%m-%d") if l["closed_at"] else ""
        key = (cid, closed_day)
        if cid and key in seen_venda_keys:
            l["venda"] = False; l["duplicated"] = True; venda_dups += 1
        else:
            seen_venda_keys.add(key); l["duplicated"] = False
    if venda_dups:
        print(f"\n⚠️  {venda_dups} venda(s) duplicada(s) removida(s)")

    metrics = {
        "leads":             leads_campanha,
        "leads_sdr":         len(deduped_sdr),
        "leads_nutricao":    len(processed_nutricao),
        "leads_rd_sdr":      len(processed_rd_sdr),
        "leads_rd_closer":   len(processed_rd_closer),
        "qualified":         sum(1 for l in deduped_sdr if l["qualified"]),
        "reuniao_agendada":  sum(1 for l in deduped_sdr if l["reuniao_agendada"]),
        "reuniao_realizada": sum(1 for l in deduped_sdr if l["reuniao_realizada"]),
        "proposta":          sum(1 for l in processed_closer if l["proposta"]),
        "venda":             sum(1 for l in processed_closer if l["venda"]),
        "receita":           sum(l["price"] for l in processed_closer if l["venda"]),
        "perda":             (sum(1 for l in deduped_sdr if l["perda"]) +
                              sum(1 for l in processed_closer if l["perda"] and l["closed_in_period"]) +
                              sum(1 for l in processed_nutricao if l["perda"]) +
                              sum(1 for l in processed_rd_sdr if l["perda"]) +
                              sum(1 for l in processed_rd_closer if l["perda"])),
        "vendas_duplicadas_removidas": venda_dups,
    }
    print("\n📊 MÉTRICAS:")
    for k, v in metrics.items(): print(f"  {k:22s}: {v}")

    output = {
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
        "account":     SUBDOMAIN,
        "period":      {"start": str(PERIOD_START), "end": str(PERIOD_END)},
        "status_map":  {str(k): v for k, v in status_map.items()},
        "loss_reasons":{str(k): v for k, v in loss_reasons_map.items()},
        "users_map":   {str(k): v for k, v in users_map.items()},
        "task_types":  {str(k): v for k, v in task_types.items()},
        "tasks":       tasks,
        "metrics":     metrics,
        "sdr":         [slim(l) for l in deduped_sdr],
        "closer":      [slim(l) for l in processed_closer],
        "nutricao":    [slim(l) for l in processed_nutricao],
        # Base RD entra agregada — ver rd_summary().
        "rd_summary": {
            "Importação RD (SDR)": rd_summary(processed_rd_sdr),
            "Base Closer RD":      rd_summary(processed_rd_closer),
        },
    }

    out_path = Path(__file__).resolve().parent / "data/kommo_leads.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    mb = out_path.stat().st_size / 1_048_576
    print(f"\n✅ Salvo em {out_path} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
