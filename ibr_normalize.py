"""
Normalização compartilhada entre Kommo e Meta Ads.

O dashboard cruza o lead (Kommo) com o gasto (Meta) por **público** e por
**criativo**. Os dois lados escrevem o mesmo público/criativo de formas
diferentes — o Kommo recebe o slug da UTM ('publico_interesses_alto_padrão')
e o Meta guarda o nome do adset/anúncio ('00 - [INTERESSES] - ALTO PADRÃO').

Este módulo reduz os dois lados ao mesmo rótulo canônico. É a única fonte de
verdade dessa tradução: `fetch_kommo_ibr.py` e `fetch_meta_spend.py` importam
daqui, então mudar uma regra corrige os dois lados de uma vez.
"""

import re
import unicodedata

NAO_RASTREADO = "Não rastreado"
SEM_DADO = "—"


def slug(s):
    """minúsculo, sem acento, separadores virando '_'."""
    if not s:
        return ""
    s = str(s).replace("+", " ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# Placeholders de UTM que chegaram sem substituição ('{{campaign.name}}',
# '(not set)'…). São ausência de dado, não um público/criativo chamado assim.
_PLACEHOLDERS = {"campaign_name", "ad_name", "adset_name", "site_source_name",
                 "placement", "not_set", "notset", "unknown", "referral", "none", "null"}


def _is_placeholder(s):
    return (not s) or s in _PLACEHOLDERS


# ═══════════════════════════════════════════════════════════════════════════
#  Canal
# ═══════════════════════════════════════════════════════════════════════════

def normalize_canal(origem):
    """Campo 'Origem' do contato → canal canônico.

    Valores reais na conta: 'Meta+Ads', 'Facebook', 'instagram', 'google',
    'Google', 'hablla.io', '(referral)', 'unknown', ''.
    """
    s = slug(origem)
    if not s or s in {"unknown", "none", "null", "nao_definido", "not_set",
                      "fonte_sem_fonte", "sem_fonte"}:
        return NAO_RASTREADO
    # Alguns contatos vieram com data ou número no campo Origem (erro de
    # preenchimento na integração) — isso é ausência de rastreio, não canal.
    if not any(c.isalpha() for c in s):
        return NAO_RASTREADO
    tokens = s.split("_")
    if any(k in s for k in ("meta", "facebook", "instagram")) or "fb" in tokens \
            or "ig" in tokens or "an" in tokens:   # 'an' = audience network
        return "Meta Ads"
    if "google" in s or "gads" in tokens:
        return "Google Ads"
    if "hablla" in s:
        return "Hablla (bot)"
    if "indicacao" in s or "referral" in s:
        return "Indicação/Referral"
    if "direct" in s or "organic" in s or "site" in s:
        return "Direto/Orgânico"
    if "rd" in tokens:
        return "Base RD"
    # Origens residuais (tiktok, yahoo, chatgpt.com…) somam pouquíssimos leads;
    # agrupadas, param de poluir os rankings por canal.
    return "Outras origens"


# ═══════════════════════════════════════════════════════════════════════════
#  Público (adset)
# ═══════════════════════════════════════════════════════════════════════════
# Cada regra: (rótulo canônico, chaves do lado Meta, chaves do lado Kommo).
# A primeira regra que casar vence — ordem = prioridade.
AUDIENCE_RULES = [
    ("Remarketing",              ("remarketing", "pageview", "rmkt"),
                                 ("rmkt", "remarketing", "pageview")),
    ("LAL 1% · Leads A",         ("lookalike_1_leads_a", "semelhante_leads_a",
                                  "lista_semelhantes_leads_a", "leads_a"),
                                 # 'fundo_de_funil' entra aqui porque a campanha
                                 # 'Fundo de Funil | Leads | IBR' roda com um
                                 # único adset — o Lookalike 1% leads A.
                                 ("leads_a", "lookalike_1_leads_a",
                                  "semelhantes_leads_a", "fundo_de_funil")),
    ("LAL 1% · Oportunidades",   ("lal1", "lista_semelhantes_oportunidades", "oportunidades"),
                                 ("morno_3", "oportunidades")),
    ("LAL 3% · Leads 90D",       ("lal3",),
                                 ("morno_2",)),
    ("Interesses · Alto padrão", ("interesses", "alto_padrao"),
                                 ("interesses", "alto_padrao")),
]


def _match_audience(value, side):
    """side: 0 = chaves do Meta, 1 = chaves do Kommo."""
    s = slug(value)
    if _is_placeholder(s):
        return None
    # Portugal é um teste geográfico à parte: o adset ('EXPERIMENTAÇÃO |
    # Interesse', 'SEMELHANTE - LEADS A') repete o nome dos públicos do Brasil,
    # então a geografia tem que ser checada antes de qualquer regra de público.
    if "portugal" in s:
        return "Portugal"
    for label, meta_keys, kommo_keys in AUDIENCE_RULES:
        keys = meta_keys if side == 0 else kommo_keys
        if any(k in s for k in keys):
            return label
    return None


# Marcas que identificam campanha de FORA do Meta. As regras de público acima
# descrevem adsets do Meta; o Google não tem adset nesta conta, e o nome da
# campanha dele colide com elas: o 'rmkt' dentro de
# 'ibr_discovery_max_conv_br_01_rmkt' casa com a regra do público 'Remarketing'
# e creditava ao adset do Meta 408 leads que são do Google — e, junto com eles,
# as vendas, no ranking de "Vendas por público".
#
# A checagem é pela string da campanha, não pelo canal do lead: lead sem
# rastreio carrega o mesmo slug do Google, e lead do Meta às vezes chega com
# canal errado. Quem sabe de onde veio é o nome da campanha.
_MARCAS_FORA_DO_META = ("discovery", "max_conv", "pmax", "brandterms", "search",
                        "_sch_", "regua_de_remarketing")


def _fora_do_meta(campanha):
    s = slug(campanha)
    return any(m in s for m in _MARCAS_FORA_DO_META)


def normalize_audience_kommo(campanha):
    """Campo 'Campanha' do Kommo → público canônico.

    O campo carrega ora o adset ('publico_interesses_alto_padrão'), ora o nome
    da campanha ('IBR | CONVERSAO LP | 01 | PUBLICO MORNO 3'); as regras cobrem
    os dois. Campanha de outro canal não passa pelas regras — devolve o próprio
    nome, que para o Google é a atribuição mais fina que existe.
    """
    if _is_placeholder(slug(campanha)):
        return SEM_DADO
    if not _fora_do_meta(campanha):
        hit = _match_audience(campanha, 1)
        if hit:
            return hit
    return str(campanha).replace("+", " ").strip()


def normalize_audience_meta(adset_name, campaign_name=""):
    """Nome do adset (+ campanha como desempate) → público canônico."""
    if "portugal" in slug(campaign_name):
        return "Portugal"
    hit = _match_audience(adset_name, 0) or _match_audience(campaign_name, 0)
    if hit:
        return hit
    return (adset_name or SEM_DADO).replace("*", "").strip()


# ═══════════════════════════════════════════════════════════════════════════
#  Criativo
# ═══════════════════════════════════════════════════════════════════════════
# A nomenclatura de anúncio da conta nunca foi padronizada — '[ATIVO] [VD] LEO
# - 23.03', '[VD3] LÉO - 23.03' e 'Vídeo Leo' são o mesmo criativo. As regras
# agrupam por família (o que o anúncio mostra), não pelo texto exato.
CREATIVE_RULES = [
    ("VÍDEO · Drone",             ("drone",)),
    ("VÍDEO · Léo",               ("leo", "le0")),
    ("VÍDEO · IA",                ("ia_12_02", "video_ia", "vd_ia")),
    ("VÍDEO · Tela dividida",     ("tela_dividida", "fpv")),
    ("VÍDEO · 22/06",             ("video_22_06",)),
    ("ESTÁTICO · Pé na areia",    ("pe_na_areia",)),
    ("ESTÁTICO · Férias garantidas", ("ferias_garantidas",)),
    ("ESTÁTICO · LP RD",          ("lp_rd",)),
    ("ESTÁTICO · Piscina",        ("piscina",)),
    ("ESTÁTICO · Entrada e parcela", ("entrada_e_parcela",)),
]


def _creative_620(s):
    """A peça de R$620 existe em vídeo e em estático, e o nome varia demais
    ('[VD01] [Vídeo R$620]', '[AD02] [ESTÁTICO R$620]', 'criativo_estatico_620').
    Decide pelo formato declarado no nome, não pelo texto inteiro."""
    if "620" not in s:
        return None
    tokens = s.split("_")
    if any(t.startswith("vd") or t in ("video", "videos") for t in tokens):
        return "VÍDEO · R$620"
    return "ESTÁTICO · R$620"


def normalize_creative(name):
    """Nome do anúncio (Meta) ou campo 'Anúncio' (Kommo) → criativo canônico."""
    if not name:
        return SEM_DADO
    raw = str(name).replace("+", " ").strip()
    s = slug(raw)
    if _is_placeholder(s):
        return SEM_DADO
    hit620 = _creative_620(s)
    if hit620:
        return hit620
    for label, keys in CREATIVE_RULES:
        if any(k in s for k in keys):
            return label
    # Sem regra: devolve o nome limpo (sem sufixo '| variação' e sem [ATIVO]).
    cleaned = re.sub(r"\[ativo\]\s*", "", raw, flags=re.I).strip()
    return cleaned.split(" | ")[0].strip() or SEM_DADO


# ═══════════════════════════════════════════════════════════════════════════
#  Campanha
# ═══════════════════════════════════════════════════════════════════════════
# A campanha é o nível em que a otimização acontece — é ela que se pausa, se
# escala e se realoca. Aqui ela existe só para AGRUPAR: o nome do Meta vem
# longo e repetitivo ('IBR | CONVERSAO LP | 01 | PUBLICO MORNO 3 |
# 2024-01-26'), e empilhado como cabeçalho de tabela ocupa a linha inteira sem
# informar nada. A limpeza é genérica de propósito — tira o que se repete em
# toda campanha da conta (prefixo do produto, número de ordem, data de
# criação) em vez de listar campanha por campanha, que apodrece a cada
# campanha nova.
#
# Atenção: isto NÃO é o caminho do lead até a campanha. O 'utm_campaign' que
# chega no Kommo carrega ora a campanha, ora o adset — 'morno_3_lista_
# oportunidades' e 'IBR | CONVERSAO LP | 01 | PUBLICO MORNO 3' são a mesma
# campanha vista de dois jeitos. Quem liga lead a campanha é o público, pelo
# mapa adset→campanha que vem da API (ver fetch_meta_spend.py).

# Siglas que perdem o sentido se virarem Capitalizadas.
_ACRONIMOS = {"LP", "IBR", "IBH", "IBL", "MME", "RMKT", "CBO", "ABO", "YK",
              "PMAX", "IA", "LAL", "VD", "AD"}


def _pretty_token(tok):
    """'PUBLICO MORNO 3' → 'Morno 3'. Preserva siglas e o que já é misto.

    Os colchetes viram espaço antes de tudo: 'PORTUGAL [SEMELHANTE]' é uma
    qualificação do público, não uma marcação, e mantê-los faria a sigla colar
    no colchete ('[SEMELHANTE') e escapar da checagem de maiúsculas.
    """
    tok = re.sub(r"[\[\]]", " ", tok)
    tok = re.sub(r"(?i)^\s*p[uú]blico\s+", "", tok).strip()
    palavras = []
    for p in tok.split():
        if p.upper() in _ACRONIMOS or not p.isupper():
            palavras.append(p)
        else:
            palavras.append(p.capitalize())
    return " ".join(palavras)


def normalize_campaign(name):
    """Nome da campanha (Meta ou Google) → rótulo curto de agrupamento."""
    if not name:
        return SEM_DADO
    raw = str(name).replace("+", " ").strip()
    if _is_placeholder(slug(raw)):
        return SEM_DADO
    # Data de criação no fim do nome: 'IBR | ... | 2024-01-26', '... | 13/07/2026'.
    s = re.sub(r"\s*\|?\s*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\s*$", "", raw)
    partes = []
    for p in s.split("|"):
        p = p.strip(" ]|[").strip()
        # Descarta o prefixo do produto e o número de ordem ('01', '02'), que
        # aparecem em toda campanha e não distinguem uma da outra.
        if not p or p.upper() == "IBR" or re.fullmatch(r"\d{1,2}", p):
            continue
        partes.append(_pretty_token(p))
    if not partes:
        return _pretty_token(raw)
    return " · ".join(partes).replace("Conversao", "Conversão")
