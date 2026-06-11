# BOAONDA Intelligence — instruções do projeto

## Deploy

Este app é publicado no Railway a partir do repositório GitHub
`Boaonda/boaonda-intelligence` (branch principal). O Railway faz
**redeploy automático a cada push**.

**Convenção combinada com o usuário**: ao terminar um ajuste nos
dashboards (frontend, `processador.py`, JSONs regenerados), faça
`git add` + `git commit` + `git push` automaticamente, sem perguntar
antes — não é necessário esperar confirmação para esse commit/push de
rotina. Use mensagens de commit curtas e descritivas do que mudou.

Isso é diferente do projeto `analytics-hub` (que roda só localmente via
`start.bat` — sem deploy, mudanças aparecem direto no navegador).

## Pipeline de dados

`processador.py` lê `3YS.csv` (vendas/programação/carteira) e `ESQT.xls`
(estoque) e gera os JSONs em `frontend/` (`dados_vendas.json`,
`dados_programacao.json`, `dados_refs_tabela.json`,
`dados_estoque.json`, `dados_carteira.json`, `dados_portal.json`,
`boaonda_dados_completos.json`). Sempre que a lógica de classificação
mudar, reprocessar com `processador.processar_tudo(...)` e
commitar os JSONs atualizados junto com o código.

## Pedido em Carteira

Módulo `frontend/boaonda_carteira.html` (`dados_carteira.json`, gerado por
`processador.processar_carteira`). Mostra pedidos vendidos (canais MI/ME,
E-commerce fica de fora) que ainda não têm plano de produção vinculado:

- `planoproducao` vazio/"Não se aplica"
- `cod_esp_ent_sai` em (1=Programado, 31=Venda Mista) — espécie 22 (Pronta
  Entrega) fica de fora
- `LocalEstoque == '30'` (vale para espécie 1 e 31)
- `pos_item` não é "Cancelado" nem "Faturado"

KPIs: total de pedidos/pares, canal MI/ME, em atraso / em prazo. Situação de
entrega usa o prazo médio de produção (45 dias): se `hoje + 45 dias >
dt_faturam`, o pedido não tem mais tempo hábil de produção e entra em "em
atraso" (provável atraso); caso contrário, "em prazo". Também mostra volume
por `etapa_atual` e pares por mês de `dt_entrada` e `dt_faturam`. A Home
mostra um card com os KPIs "Total de pedidos em carteira" e "Total de pares
em carteira" (`dados_portal.json.carteira`).
