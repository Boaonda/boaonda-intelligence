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

`processador.py` gera os JSONs em `frontend/` (`dados_vendas.json`,
`dados_programacao.json`, `dados_refs_tabela.json`,
`dados_estoque.json`, `dados_carteira.json`, `dados_portal.json`,
`boaonda_dados_completos.json`). Sempre que a lógica de classificação
mudar, reprocessar com `processador.processar_tudo(...)` e
commitar os JSONs atualizados junto com o código.

**Fonte de vendas/programação/carteira — MySQL interno**: se a variável de
ambiente `MYSQL_HOST` estiver definida (ver `.env`, gitignored), o
processador busca essas três bases via `carregar_linhas_3ys()` direto do
MySQL (`mould.v_entradapedidos_extended`, query `QUERY_3YS`), usando
`db_mysql.py`. Esse banco só é acessível pela rede interna — então essa
busca só funciona rodando localmente (não no Railway). Sem `MYSQL_HOST`,
cai de volta para o `3YS.csv` (upload manual via `/upload`). O mapeamento
de colunas (`IDX`) é o mesmo para os dois formatos — `_linha_de_db_row`
monta uma linha "sintética" a partir do dict do MySQL.

**Fonte de estoque**: ainda só via upload de `ESQT.xls` (`/upload`) —
migração para MySQL pendente (falta a query equivalente).

## Configuração de produção (`config_producao.json`)

`frontend/config_producao.json` (seedado em `DATA_DIR`, editável pela rota
`/config`) guarda `prazo_producao_dias` (padrão 45) — o lead time de
produção. É o parâmetro único que alimenta tanto a Carteira quanto a
Programação, sem precisar reprocessar dados: alterar o valor em `/config`
já reflete no próximo refresh dos dashboards.

## Pedido em Carteira

Módulo `frontend/boaonda_carteira.html` (`dados_carteira.json`, gerado por
`processador.processar_carteira`). Mostra pedidos vendidos (canais MI/ME,
E-commerce fica de fora) que ainda não têm plano de produção vinculado:

- `planoproducao` vazio/"Não se aplica"
- `cod_esp_ent_sai` em (1=Programado, 31=Venda Mista) — espécie 22 (Pronta
  Entrega) fica de fora
- `LocalEstoque == '30'` (vale para espécie 1 e 31)
- `pos_item` não é "Cancelado" nem "Faturado"

KPIs: total de pedidos/pares, canal MI/ME, em atraso / em prazo. A "situação"
é calculada **client-side** (não vem mais no JSON) a partir de `dt_faturam`
de cada pedido + `prazo_producao_dias` de `config_producao.json`: se
`hoje + prazo > dt_faturam`, o pedido não tem mais tempo hábil de produção e
entra em "em atraso" (provável atraso); caso contrário, "em prazo". Também
mostra volume por `etapa_atual` e pares por mês de `dt_entrada` e
`dt_faturam`. A Home mostra um card com os KPIs "Total de pedidos em
carteira" e "Total de pares em carteira" (`dados_portal.json.carteira`).

## Programação — semana de referência

Módulo `frontend/boaonda_programacao_v3.html`. Além de marcar a semana atual
(card "ATUAL"), busca `config_producao.json` e calcula a "semana de
referência" = segunda-feira de (hoje + `prazo_producao_dias`) — a semana que
está sendo programada agora. Esse card recebe a classe `.foco` e o badge
"PROGRAMANDO AGORA"; o período (mês) exibido por padrão é o que contém essa
semana. Header mostra "Programando para: <faixa> (prazo Nd)".
