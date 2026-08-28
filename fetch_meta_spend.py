"""
Busca gasto do Meta Ads do IBR por público (adset) e por criativo (anúncio),
quebrados por campanha. Saída: data/meta_spend.json

Os rótulos de público e criativo passam por ibr_normalize.py — o mesmo módulo
usado no fetch_kommo_ibr.py — para que gasto e lead cheguem à mesma chave e o
dashboard consiga calcular CPL, CPL qualificado, CAC e ROAS por público/criativo.
"""

import json, os, re, requests
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

from ibr_normalize import (normalize_audience_meta, normalize_creative,
                           normalize_campaign)


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
        "fields":         f"{level}_name,campaign_name,spend,impressions,clicks",
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


def fetch_entities(endpoint, extra_fields=""):
    """Status (ACTIVE/PAUSED/...) e metadados de adsets ou ads."""
    rows, url = [], f"{API}/{ACCOUNT}/{endpoint}"
    fields = "name,effective_status,status" + (f",{extra_fields}" if extra_fields else "")
    params = {"access_token": TOKEN, "fields": fields, "limit": 500}
    while url:
        data = requests.get(url, params=params, timeout=90).json()
        if "error" in data:
            print(f"  ❌ {endpoint}: {data['error'].get('message')}")
            break
        rows.extend(data.get("data", []))
        url, params = data.get("paging", {}).get("next"), {}
    return rows


def _fetch_iframe_src(ad_id):
    """Extrai o src do iframe de preview — usado no modal 'ver criativo'."""
    try:
        r = requests.get(f"{API}/{ad_id}/previews",
                         params={"access_token": TOKEN, "ad_format": "DESKTOP_FEED_STANDARD"},
                         timeout=15)
        body = r.json().get("data", [{}])[0].get("body", "") if r.ok else ""
        m = re.search(r"src=['\"]([^'\"]+)['\"]", body)
        return m.group(1).replace("&amp;", "&") if m else ""
    except Exception:
        return ""


def _round(d):
    return {k: {ds: round(v, 2) for ds, v in days.items() if v} for k, days in d.items()}


def _round2(d):
    """Mesma coisa um nível abaixo: {campanha: {chave: {dia: gasto}}}."""
    return {c: _round(inner) for c, inner in d.items()}


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
    aud_imp, aud_clk = defaultdict(float), defaultdict(float)
    # Mesmo gasto, quebrado por campanha. É o que permite ao dashboard mostrar
    # a campanha como agrupador em vez de somar tudo numa linha só.
    aud_by_camp  = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    camp_spend   = defaultdict(lambda: defaultdict(float))
    for r in fetch_insights("adset"):
        key  = normalize_audience_meta(r.get("adset_name", ""), r.get("campaign_name", ""))
        camp = normalize_campaign(r.get("campaign_name", ""))
        ds, v = r.get("date_start", ""), float(r.get("spend", 0))
        aud_spend[key][ds] += v
        aud_by_camp[camp][key][ds] += v
        # O total da campanha é a soma dos seus adsets: no Meta o gasto do
        # adset particiona o da campanha, então não há por que pedir de novo.
        camp_spend[camp][ds] += v
        aud_imp[key] += float(r.get("impressions", 0) or 0)
        aud_clk[key] += float(r.get("clicks", 0) or 0)
    print(f"  {len(aud_spend)} públicos:")
    for k, days in sorted(aud_spend.items(), key=lambda x: -sum(x[1].values())):
        print(f"    R$ {sum(days.values()):>10,.2f}  {k}")

    print("\nInsights por anúncio (criativo)...")
    cri_spend = defaultdict(lambda: defaultdict(float))
    cri_imp, cri_clk = defaultdict(float), defaultdict(float)
    cri_by_camp = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for r in fetch_insights("ad"):
        key  = normalize_creative(r.get("ad_name", ""))
        camp = normalize_campaign(r.get("campaign_name", ""))
        ds, v = r.get("date_start", ""), float(r.get("spend", 0))
        cri_spend[key][ds] += v
        cri_by_camp[camp][key][ds] += v
        cri_imp[key] += float(r.get("impressions", 0) or 0)
        cri_clk[key] += float(r.get("clicks", 0) or 0)
    print(f"  {len(cri_spend)} criativos em {len(cri_by_camp)} campanha(s):")
    for k, days in sorted(cri_spend.items(), key=lambda x: -sum(x[1].values())):
        n_camp = sum(1 for c in cri_by_camp.values() if c.get(k))
        print(f"    R$ {sum(days.values()):>10,.2f}  {k}  ({n_camp} campanha(s))")

    print("\nStatus dos adsets...")
    aud_status = {}
    for row in fetch_entities("adsets"):
        key = normalize_audience_meta(row.get("name", ""))
        st  = row.get("effective_status", "UNKNOWN")
        # Vários adsets caem no mesmo público — basta um ativo pro público estar ativo.
        if key not in aud_status or st == "ACTIVE":
            aud_status[key] = st

    print("Status das campanhas...")
    camp_status = {}
    for row in fetch_entities("campaigns"):
        key = normalize_campaign(row.get("name", ""))
        st  = row.get("effective_status", "UNKNOWN")
        if key not in camp_status or st == "ACTIVE":
            camp_status[key] = st

    print("Status e preview dos anúncios...")
    cri_status, cri_preview, ad_id_by_key = {}, {}, {}
    for row in fetch_entities("ads", extra_fields="preview_shareable_link,id"):
        key = normalize_creative(row.get("name", ""))
        st  = row.get("effective_status", "UNKNOWN")
        prv, aid = row.get("preview_shareable_link", ""), row.get("id", "")
        if key not in cri_status or st == "ACTIVE":
            cri_status[key] = st
            if prv: cri_preview[key] = prv
            if aid: ad_id_by_key[key] = aid
        elif key not in cri_preview and prv:
            cri_preview[key] = prv
            ad_id_by_key.setdefault(key, aid)

    print(f"Buscando iframe de preview de {len(ad_id_by_key)} criativos...")
    cri_iframe = {}
    for key, aid in ad_id_by_key.items():
        src = _fetch_iframe_src(aid)
        if src: cri_iframe[key] = src
    print(f"  {len(cri_iframe)} iframes")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "account":    ACCOUNT,
        "period":     {"since": SINCE, "until": UNTIL},
        "adset":      _round(aud_spend),      # {público: {'YYYY-MM-DD': gasto}}
        "creative":   _round(cri_spend),      # {criativo: {'YYYY-MM-DD': gasto}}
        "campaign":   _round(camp_spend),     # {campanha: {'YYYY-MM-DD': gasto}}
        "adset_by_campaign":    _round2(aud_by_camp),   # {campanha: {público: {dia: gasto}}}
        "creative_by_campaign": _round2(cri_by_camp),   # {campanha: {criativo: {dia: gasto}}}
        "audience_campaign":    _dominant_campaign_by_day(aud_by_camp),
        "campaign_status":      camp_status,
        "adset_totals":    {k: {"impressions": aud_imp[k], "clicks": aud_clk[k]} for k in aud_spend},
        "creative_totals": {k: {"impressions": cri_imp[k], "clicks": cri_clk[k]} for k in cri_spend},
        "adset_status":    aud_status,
        "creative_status": cri_status,
        "creative_preview": cri_preview,
        "creative_preview_iframe": cri_iframe,
    }

    out_path = Path(__file__).resolve().parent / "data/meta_spend.json"
    # Se a API voltou vazia (token expirado), preserva o arquivo anterior em vez
    # de publicar um dashboard zerado.
    if not aud_spend and not cri_spend and out_path.exists():
        print("\n⚠️  API retornou vazio (token expirado?). Mantendo dados anteriores.")
        return

    # Status e preview vêm de endpoints separados que estouram rate limit com
    # facilidade. Quando falham, o gasto (que é o essencial) já veio — então
    # reaproveita o que o arquivo anterior tinha em vez de zerar os previews.
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            for k in ("adset_status", "creative_status", "campaign_status",
                      "creative_preview", "creative_preview_iframe"):
                if not out[k] and prev.get(k):
                    out[k] = prev[k]
                    print(f"  ↺ {k} preservado do arquivo anterior ({len(prev[k])} itens)")
        except Exception as e:
            print(f"  ⚠️  não consegui ler o arquivo anterior: {e}")

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"\n✅ Salvo em {out_path} · investimento total R$ {_total(aud_spend):,.2f}")


if __name__ == "__main__":
    main()
