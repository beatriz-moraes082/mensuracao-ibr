# Mensuração · Ipioca Beach Residence

Dashboard de mídia paga e funil comercial do **Ipioca Beach Residence**, cruzando
CRM (Kommo) com investimento de **Meta Ads** e **Google Ads**.

**No ar:** https://beatriz-moraes082.github.io/mensuracao-ibr/

Os dados se atualizam sozinhos **às 7h e às 14h** (horário de Brasília). Não é
tempo real: o rodapé do dash mostra a data da última coleta.

## Como funciona

```
fetch_kommo_ibr.py    ─┐
fetch_meta_spend.py    ├─→  data/*.json  ─→  index.html  ─→  GitHub Pages
fetch_google_spend.py ─┘
```

Um workflow do GitHub Actions roda os três coletores, confere se os dados vieram
inteiros e commita `data/` de volta no `main`. O Pages republica sozinho a cada
push. Se uma coleta falhar, nada é publicado e o dash continua mostrando os
últimos dados bons — com a data no rodapé denunciando que envelheceu.

| Arquivo | O que faz |
|---|---|
| [`index.html`](./index.html) | O dashboard inteiro — página única, sem build |
| [`fetch_kommo_ibr.py`](./fetch_kommo_ibr.py) | Leads dos funis SDR, Closer e Nutrição, tarefas e status |
| [`fetch_meta_spend.py`](./fetch_meta_spend.py) | Gasto do Meta por adset e por criativo, dia a dia |
| [`fetch_google_spend.py`](./fetch_google_spend.py) | Gasto do Google por campanha, dia a dia |
| [`ibr_normalize.py`](./ibr_normalize.py) | Normaliza canal, público e criativo a partir das UTMs |
| [`sobe_secrets.py`](./sobe_secrets.py) | Envia credenciais do `.env` para os secrets do GitHub |
| [`google_oauth_setup.py`](./google_oauth_setup.py) | Gera o `refresh_token` do Google Ads |

## Rodar fora de hora

Não precisa de nada instalado — dispara o mesmo workflow lá no GitHub:

```bash
gh workflow run "Atualiza dados do dashboard" --repo beatriz-moraes082/mensuracao-ibr
```

Para conferir a saúde das últimas execuções:

```bash
gh run list --repo beatriz-moraes082/mensuracao-ibr --limit 5
```

## Credenciais

Ficam nos **secrets do GitHub**, nunca no repositório. O `.env` local só é
necessário para rodar os coletores na própria máquina.

| Secret | Origem | Expira? |
|---|---|---|
| `KOMMO_TOKEN` | Kommo → Integrações → chave de longa duração | já foi revogado sem aviso; se der 401, gere outro |
| `META_TOKEN` | Meta Business | ~60 dias |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | [API Center](https://ads.google.com/aw/apicenter) da MCC | não |
| `GOOGLE_ADS_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN` | `google_oauth_setup.py` | não |

Trocar uma credencial, num passo só (pede, valida na API e só então grava e envia):

```bash
python3 sobe_secrets.py --novo KOMMO_TOKEN
```

Ele se recusa a enviar um valor que a API rejeita — assim um `.env` desatualizado
não sobrescreve um secret que está funcionando.

## O que os números significam

O botão **Metodologia**, no topo do dash, define cada indicador e mostra a
fórmula. Vale destacar três coisas que não são óbvias:

- **Venda** é só o status `142` do funil **Closer**. O mesmo `142` no SDR e na
  Importação RD significa "reunião realizada" e não entra.
- **CAC e ROAS** são teto e piso, não valores exatos: parte das vendas entra sem
  rastreio e fica fora do numerador/denominador. A tabela **Eficiência
  financeira por canal**, no Diagnóstico, reparte essa escada — CPL, CPL
  qualificado, CPO, CAC e ROAS — por origem, e herda a mesma ressalva.
- **CPO** é custo por *oportunidade*, e oportunidade é **reunião realizada**.
  Não confundir com o KPI *Custo por reunião* do topo: aquele divide o gasto
  total por todas as reuniões, inclusive as de lead orgânico, e por isso é um
  piso; o CPO por canal só cruza gasto e reunião da mesma origem.
- **Ritmo e Prazo** usam a última alteração da tarefa como aproximação da
  conclusão — o Kommo não expõe data de conclusão.

## Limitações conhecidas

- **Junho/2026** está fora do comparativo mensal: o apagão de rastreio de 17/06 a
  10/07 deixou os leads sem UTM, então o CPL do mês mediria o rastreio, não a
  mídia. Os leads seguem no dash, no filtro "Tudo".
- **A tabela semanal** (aba Evolução) tem seletor de canal, que vale só para
  ela — os gráficos e KPIs da aba seguem mostrando o período inteiro.
- **Público e criativo** cobrem só o Meta. O Google entra no CPL, CAC e ROAS do
  topo, mas ainda não no recorte por público.
- **Custo por canal existe só onde há API de mídia.** Meta e Google têm gasto
  medido; orgânico, indicação e "não rastreado" ficam com as colunas de custo em
  branco — o custo deles existe, só não passa por aqui.
- **~4% do gasto do Google** não casa com lead: algumas campanhas chegam com
  `utm_campaign` sem o sufixo de data, ambíguo entre duas campanhas. Resolve-se
  padronizando a UTM no Google Ads.

## Privacidade

O JSON publicado é agregado. Telefone e e-mail de lead são mascarados **na
origem**, antes de sair do coletor, e o repositório não guarda e-mail do time.
