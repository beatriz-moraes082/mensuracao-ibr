"""
Busca gasto do Meta Ads do IBR por campanha e por público (adset).
Saída: data/meta_spend.json

Os rótulos passam por ibr_normalize.py — o mesmo módulo usado no
fetch_kommo_ibr.py — para que gasto e lead cheguem à mesma chave e o dashboard
consiga calcular CPL, CPL qualificado e CAC por campanha.

Não busca nível de anúncio. O dashboard deixou de ter recorte por criativo, e
manter a coleta significaria oito varreduras a mais na API e 184 requisições de
preview a cada rodada, para gravar 180 KB que ninguém lê. O histórico do git
tem o código, se o recorte voltar.
"""

import json, os, requests
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

from ibr_normalize import normalize_audience_meta, normalize_campaign


def _load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists(): return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

TOKEN   = os.environ["META_TOKEN"]
ACCOUNT = os.environ["META_ACCOUNT"]
API     = "https://graph.facebook.com/v21.0"

# Mesmo período do Kommo — o dashboard cruza os dois pela mesma janela.
SINCE = "2026-01-01"
UNTIL = date.today().isoformat()


def _month_chunks(since_str, until_str):
    """Divide o período em janelas mensais.

    Contorna o bug #2642 ('Invalid cursors') que a API do Meta devolve ao
    paginar períodos longos com time_increment=1.
    """
    start = datetime.fromisoformat(since_str).date()
    end   = datetime.fromisoformat(until_str).date()
    chunks, cur = [], start
    while cur <= end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        chunk_end = min(nxt - timedelta(days=1), end)
        chunks.append((cur.isoformat(), chunk_end.isoformat()))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _fetch_window(level, since, until):
    rows, url = [], f"{API}/{ACCOUNT}/insights"
    params = {
        "access_token":   TOKEN,
        "level":          level,
        "fields":         f"{level}_name,campaign_name,spend",
        "time_range":     f'{{"since":"{since}","until":"{until}"}}',
        "time_increment": 1,
        "limit":          500,
    }
    while url:
        data = requests.get(url, params=params, timeout=90).json()
        if "error" in data:
            print(f"  ❌ {since}→{until}: {data['error'].get('message')}")
            break
        rows.extend(data.get("data", []))
        url, params = data.get("paging", {}).get("next"), {}
    return rows


def fetch_insights(level):
    print(f"  account={ACCOUNT} level={level} {SINCE}→{UNTIL}")
    rows = []
    for since, until in _month_chunks(SINCE, UNTIL):
        chunk = _fetch_window(level, since, until)
        rows.extend(chunk)
        print(f"    {since}→{until}: {len(chunk)} linhas")
    return rows


def fetch_entities(endpoint):
    """Status (ACTIVE/PAUSED/...) das entidades de um endpoint."""
    rows, url = [], f"{API}/{ACCOUNT}/{endpoint}"
    params = {"access_token": TOKEN, "fields": "name,effective_status,status", "limit": 500}
    while url:
        data = requests.get(url, params=params, timeout=90).json()
        if "error" in data:
            print(f"  ❌ {endpoint}: {data['error'].get('message')}")
            break
        rows.extend(data.get("data", []))
        url, params = data.get("paging", {}).get("next"), {}
    return rows


def _round(d):
    return {k: {ds: round(v, 2) for ds, v in days.items() if v} for k, days in d.items()}


def _dominant_campaign_by_day(by_camp):
    """{campanha: {público: {dia: gasto}}} → {público: {dia: campanha}}.

    É este mapa que liga LEAD a campanha no dashboard. O 'utm_campaign' que
    chega no Kommo não serve: metade dos leads traz o nome do adset
    ('morno_3_lista_oportunidades') e metade o da campanha — os dois são a
    mesma campanha escrita de dois jeitos. O que o lead tem de confiável é o
    público, então a campanha vem daqui: no dia em que o lead entrou, qual
    campanha estava pagando por aquele público.

    Quando duas campanhas disputam o mesmo público no mesmo dia (acontece na
    virada de uma campanha para a outra), vence a que gastou mais naquele dia
    — é a que produziu a maior parte dos leads do dia.
    """
    por_publico = defaultdict(lambda: defaultdict(dict))
    for camp, pubs in by_camp.items():
        for pub, days in pubs.items():
            for ds, v in days.items():
                if v:
                    por_publico[pub][ds][camp] = v
    return {pub: {ds: max(camps.items(), key=lambda x: x[1])[0]
                  for ds, camps in dias.items()}
            for pub, dias in por_publico.items()}


def _total(days_by_key):
    return sum(v for days in days_by_key.values() for v in days.values())


def main():
    print("=== Meta Ads · IBR ===")

    # O gasto é guardado por DIA (não por semana): o dashboard filtra por
    # janelas arbitrárias — "últimos 7 dias", "mês passado", período custom —
    # e só com o diário o investimento da janela bate com o real.
    print("Insights por adset (público)...")
    aud_spend = defaultdict(lambda: defaultdict(float))
    # Gasto quebrado por campanha. Não vai para o arquivo: serve para somar o
    # total da campanha e para descobrir, dia a dia, qual campanha estava
    # pagando por cada público — que é como o lead chega até a campanha dele.
    aud_by_camp = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    camp_spend  = defaultdict(lambda: defaultdict(float))
    for r in fetch_insights("adset"):
        key  = normalize_audience_meta(r.get("adset_name", ""), r.get("campaign_name", ""))
        camp = normalize_campaign(r.get("campaign_name", ""))
        ds, v = r.get("date_start", ""), float(r.get("spend", 0))
        aud_spend[key][ds] += v
        aud_by_camp[camp][key][ds] += v
        # O total da campanha é a soma dos seus adsets: no Meta o gasto do
        # adset particiona o da campanha, então não há por que pedir de novo.
        camp_spend[camp][ds] += v
    print(f"  {len(aud_spend)} públicos em {len(camp_spend)} campanha(s):")
    for k, days in sorted(aud_spend.items(), key=lambda x: -sum(x[1].values())):
        print(f"    R$ {sum(days.values()):>10,.2f}  {k}")

    print("\nStatus das campanhas...")
    camp_status = {}
    for row in fetch_entities("campaigns"):
        key = normalize_campaign(row.get("name", ""))
        st  = row.get("effective_status", "UNKNOWN")
        if key not in camp_status or st == "ACTIVE":
            camp_status[key] = st
    print(f"  {len(camp_status)} campanhas")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "account":    ACCOUNT,
        "period":     {"since": SINCE, "until": UNTIL},
        "adset":      _round(aud_spend),      # {público: {'YYYY-MM-DD': gasto}}
        "campaign":   _round(camp_spend),     # {campanha: {'YYYY-MM-DD': gasto}}
        "audience_campaign": _dominant_campaign_by_day(aud_by_camp),
        "campaign_status":   camp_status,
    }

    out_path = Path(__file__).resolve().parent / "data/meta_spend.json"
    # Se a API voltou vazia (token expirado), preserva o arquivo anterior em vez
    # de publicar um dashboard zerado.
    if not aud_spend and out_path.exists():
        print("\n⚠️  API retornou vazio (token expirado?). Mantendo dados anteriores.")
        return

    # O status vem de um endpoint separado, que estoura rate limit com muito
    # mais facilidade que o de insights. Quando ele falha, o gasto — que é o
    # essencial — já veio; então reaproveita o status do arquivo anterior em
    # vez de publicar tudo sem selo de ativa/pausada.
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            if not out["campaign_status"] and prev.get("campaign_status"):
                out["campaign_status"] = prev["campaign_status"]
                print(f"  ↺ campaign_status preservado do arquivo anterior "
                      f"({len(prev['campaign_status'])} itens)")
        except Exception as e:
            print(f"  ⚠️  não consegui ler o arquivo anterior: {e}")

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"\n✅ Salvo em {out_path} · investimento total R$ {_total(aud_spend):,.2f}")


if __name__ == "__main__":
    main()
