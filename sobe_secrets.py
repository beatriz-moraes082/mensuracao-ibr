#!/usr/bin/env python3
"""Envia para os secrets do GitHub as credenciais que estão no .env local.

    python3 sobe_secrets.py                 todas as chaves conhecidas
    python3 sobe_secrets.py KOMMO_TOKEN     só a que você nomear
    python3 sobe_secrets.py --force ...     envia mesmo se a checagem falhar

Antes de enviar, testa a credencial contra a API de origem. Um secret que já
funciona no GitHub não pode ser substituído por um valor local quebrado — foi
assim que o .env desatualizado derrubou a coleta por três dias.

Nenhum valor é impresso: só o nome da chave, o tamanho e o resultado.
O developer token do Google não vive no .env; para ele use
    gh secret set GOOGLE_ADS_DEVELOPER_TOKEN --repo <repo>
"""

import subprocess
import sys
from pathlib import Path

import requests

REPO = "beatriz-moraes082/mensuracao-ibr"
ENV = Path(__file__).resolve().parent / ".env"

CHAVES_PADRAO = [
    "KOMMO_TOKEN",
    "META_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
]


def le_env():
    if not ENV.exists():
        raise SystemExit(f"Não achei o .env em {ENV}")
    vals = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def checa_kommo(env):
    sub = env.get("KOMMO_SUBDOMAIN", "").strip()
    tok = env.get("KOMMO_TOKEN", "").strip()
    if not sub or not tok:
        return None, "sem KOMMO_SUBDOMAIN ou KOMMO_TOKEN no .env"
    r = requests.get(f"https://{sub}.kommo.com/api/v4/account",
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    return r.ok, f"a API do Kommo respondeu {r.status_code}"


def checa_meta(env):
    tok = env.get("META_TOKEN", "").strip()
    acc = env.get("META_ACCOUNT", "").strip()
    if not tok or not acc:
        return None, "sem META_TOKEN ou META_ACCOUNT no .env"
    r = requests.get(f"https://graph.facebook.com/v21.0/{acc}",
                     params={"fields": "name", "access_token": tok}, timeout=30)
    return r.ok, f"a API do Meta respondeu {r.status_code}"


def checa_google_refresh(env):
    cid = env.get("GOOGLE_ADS_CLIENT_ID", "").strip()
    sec = env.get("GOOGLE_ADS_CLIENT_SECRET", "").strip()
    ref = env.get("GOOGLE_ADS_REFRESH_TOKEN", "").strip()
    if not (cid and sec and ref):
        return None, "faltam client_id/secret/refresh no .env"
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": sec,
        "refresh_token": ref, "grant_type": "refresh_token",
    }, timeout=60)
    return r.ok, f"o OAuth do Google respondeu {r.status_code}"


# Chave -> função que diz se o valor local ainda é aceito pela origem.
# Sem checagem cadastrada, a chave sobe (nada a validar sozinha).
CHECAGENS = {
    "KOMMO_TOKEN": checa_kommo,
    "META_TOKEN": checa_meta,
    "GOOGLE_ADS_REFRESH_TOKEN": checa_google_refresh,
    "GOOGLE_ADS_CLIENT_ID": checa_google_refresh,
    "GOOGLE_ADS_CLIENT_SECRET": checa_google_refresh,
}


def envia(chave, valor):
    p = subprocess.run(["gh", "secret", "set", chave, "--repo", REPO],
                       input=valor, text=True, capture_output=True)
    return p.returncode == 0, (p.stderr or "").strip()


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    env = le_env()
    chaves = args or CHAVES_PADRAO

    cache = {}
    falhas = 0
    for k in chaves:
        v = env.get(k, "").strip()
        if not v:
            print(f"  {k:<28} sem valor no .env — pulado")
            continue

        fn = CHECAGENS.get(k)
        if fn:
            if fn not in cache:
                cache[fn] = fn(env)
            ok, detalhe = cache[fn]
            if ok is False and not force:
                print(f"  {k:<28} NÃO ENVIADO — {detalhe}")
                print(f"  {'':<28} o valor local está inválido; enviar sobrescreveria")
                print(f"  {'':<28} um secret que talvez esteja bom no GitHub.")
                falhas += 1
                continue
            if ok is False:
                print(f"  {k:<28} inválido ({detalhe}) — enviando por --force")

        sucesso, err = envia(k, v)
        if sucesso:
            print(f"  {k:<28} enviado ({len(v)} caracteres)")
        else:
            print(f"  {k:<28} FALHOU — {err[:80]}")
            falhas += 1

    print("\nSecrets agora no repositório:")
    subprocess.run(["gh", "secret", "list", "--repo", REPO])
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
