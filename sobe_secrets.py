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


def grava_env(chave, valor):
    """Reescreve só essa chave, preservando o resto do arquivo."""
    import re
    txt = ENV.read_text()
    txt, n = re.subn(rf"(?m)^{re.escape(chave)}=.*$", lambda m: f"{chave}={valor}", txt)
    if n == 0:
        txt = txt.rstrip("\n") + f"\n{chave}={valor}\n"
    ENV.write_text(txt)
    return n


def modo_novo(chave):
    """Pede um valor novo, valida, e só então grava no .env e envia ao GitHub.

    Num passo só: separado em dois, é fácil achar que gravou quando não gravou.
    """
    import getpass

    env = le_env()
    print(f"\n  Novo valor para {chave}")
    print("  A digitação não aparece na tela. Cole e tecle Enter.\n")
    valor = getpass.getpass(f"  {chave}: ").strip()
    if not valor:
        raise SystemExit("  Nada recebido — nada foi alterado.")
    print(f"\n  Recebi {len(valor)} caracteres.")

    anterior = env.get(chave, "").strip()
    if valor == anterior:
        raise SystemExit("  É idêntico ao que já está no .env — nada foi alterado.\n"
                         "  Confira se o Kommo gerou uma chave nova em vez de reexibir a antiga.")

    fn = CHECAGENS.get(chave)
    if fn:
        ok, detalhe = fn({**env, chave: valor})
        if not ok:
            raise SystemExit(f"  Recusado: {detalhe}\n"
                             f"  Nada foi gravado nem enviado.")
        print(f"  Validado: {detalhe}")

    grava_env(chave, valor)
    print("  Gravado no .env.")

    sucesso, err = envia(chave, valor)
    if not sucesso:
        raise SystemExit(f"  Gravado localmente, mas o envio ao GitHub falhou: {err[:120]}")
    print("  Enviado ao GitHub.\n")

    subprocess.run(["gh", "secret", "list", "--repo", REPO])
    return 0


def envia(chave, valor):
    p = subprocess.run(["gh", "secret", "set", chave, "--repo", REPO],
                       input=valor, text=True, capture_output=True)
    return p.returncode == 0, (p.stderr or "").strip()


def main():
    argv = sys.argv[1:]
    if "--novo" in argv:
        i = argv.index("--novo")
        if i + 1 >= len(argv):
            raise SystemExit("uso: python3 sobe_secrets.py --novo KOMMO_TOKEN")
        return modo_novo(argv[i + 1])

    args = [a for a in argv if a != "--force"]
    force = "--force" in argv
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
