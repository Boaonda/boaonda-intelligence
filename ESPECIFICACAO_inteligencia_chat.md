# Especificação — Aba "Inteligência" (Chat com IA sobre os dados do portal)

## Objetivo

Criar uma aba de chat dentro do Boaonda Intelligence onde o usuário pode fazer perguntas em linguagem natural sobre os dados do portal — programação, ocupação, vendas, estoque, faturamento, carteira — e receber respostas analíticas fundamentadas nos dados reais, sem precisar navegar pelos dashboards.

---

## 1. Como funciona (arquitetura)

```
Usuário digita pergunta no portal
        ↓
Frontend identifica quais JSONs são relevantes para a pergunta
        ↓
POST /api/inteligencia  →  { pergunta, contexto (JSONs selecionados) }
        ↓
app.py monta o prompt com os dados e chama a API Anthropic (claude-sonnet-4-6)
        ↓
Resposta em linguagem natural + dados de suporte
        ↓
Frontend exibe a resposta com formatação visual
```

**Importante:** o estoque tem 240k chars (~60k tokens) — enviar o JSON completo em toda pergunta seria caro e lento. A solução é **seleção inteligente de contexto** — cada pergunta recebe apenas os JSONs relevantes (ver seção 3).

---

## 2. Endpoint no backend

### `POST /api/inteligencia`

**Request:**
```json
{
  "pergunta": "Qual semana de julho tem mais folga para encaixar novos pedidos?",
  "contextos": ["ocupacao", "programacao"]
}
```

**Lógica no app.py:**
1. Receber pergunta e lista de contextos
2. Carregar os JSONs solicitados do `frontend/` (já existem no servidor)
3. Montar o system prompt com os dados + instruções de análise
4. Chamar `anthropic.messages.create()` com `claude-sonnet-4-6`
5. Retornar a resposta

**Response:**
```json
{
  "resposta": "A semana com mais folga em julho é 27/Jul–31/Jul...",
  "contextos_usados": ["ocupacao", "programacao"],
  "tokens_usados": 3420,
  "custo_estimado_usd": 0.08
}
```

---

## 3. Seleção inteligente de contexto

Para evitar enviar todos os dados em toda pergunta, o frontend detecta palavras-chave e seleciona os JSONs relevantes:

| Palavras-chave na pergunta | JSONs enviados |
|---|---|
| semana, produção, programação, meta | `dados_programacao.json` |
| ocupação, eficiência, gargalo, capacidade | `dados_ocupacao_semanal.json` |
| estoque, disponível, livre, grades | `dados_estoque.json` (resumo) |
| vendas, pedidos, canal, MI, ME | `dados_vendas.json` |
| faturamento, receita, previsto, realizado | `dados_faturamento.json` |
| carteira, pedido aberto, etapa | `dados_carteira.json` |
| referência, produto, par | `dados_refs_tabela.json` |

**Regra de fallback:** se nenhuma palavra-chave for detectada, enviar `dados_portal.json` (o JSON resumo da home — pequeno e abrangente).

**Estoque:** nunca enviar o JSON completo. Criar uma versão resumida em tempo real: só `totais` + top 15 referências por estoque livre.

---

## 4. System prompt (instruções para o modelo)

```
Você é o assistente de inteligência do Boaonda Intelligence, 
sistema de gestão da Boaonda Calçados (Mould Indústria de Matrizes Ltda, 
Sapiranga/RS).

Você tem acesso aos dados reais do portal abaixo e deve responder 
perguntas do gestor de forma direta, analítica e em português brasileiro.

REGRAS:
- Responda sempre baseado nos dados fornecidos, nunca invente números
- Seja direto: comece com a resposta, depois explique o raciocínio
- Use linguagem de negócios (pares, semana, meta, ocupação, gargalo)
- Quando identificar um problema ou oportunidade, sinalize claramente
- Formato: texto corrido, sem markdown excessivo, máximo 4 parágrafos
- Se os dados não forem suficientes para responder, diga claramente

CONTEXTO DO NEGÓCIO:
- Meta semanal de produção: 30.000 pares
- Canais de venda: MI (Mercado Interno), ME (Mercado Externo), EC (E-commerce)
- Espécies MI: Programação (cod 1), Pronta Entrega (cod 22), Venda Mista (cod 31)
- Teto do atelier: 29.000 pares/semana convencional + 6.000 montado
- Gargalo = indicador com maior % de ocupação entre todos os tetos

DADOS DO PORTAL:
[JSONs selecionados são inseridos aqui]
```

---

## 5. Histórico de conversa

O chat mantém histórico da sessão (não persiste entre sessões — reseta ao fechar/recarregar). Isso permite perguntas encadeadas:

```
Usuário: "Qual semana de julho tem mais folga?"
IA: "A semana 27/Jul–31/Jul tem eficiência de 65,8%..."

Usuário: "E quais referências têm mais estoque disponível?"
IA: [recebe histórico + nova pergunta]
```

**Limite:** manter últimas 6 trocas (3 perguntas + 3 respostas) no histórico para não explodir o contexto.

---

## 6. Perguntas sugeridas (chips de atalho)

Exibir chips clicáveis com perguntas frequentes para facilitar o uso:

```
[ Qual semana tem mais folga em julho? ]
[ Onde está o gargalo da produção? ]
[ Quais refs estão com estoque crítico? ]
[ Como está o faturamento previsto do mês? ]
[ Quais pedidos em carteira estão em risco? ]
[ Qual canal está crescendo mais? ]
```

---

## 7. Layout da aba

```
┌─────────────────────────────────────────────────────┐
│ IA Intelligence                    Base: 11/06/2026 │
│ Faça perguntas sobre os dados do portal             │
├─────────────────────────────────────────────────────┤
│ [Chip] [Chip] [Chip] [Chip] [Chip]                  │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │ Resposta anterior da IA (se houver)         │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │ Pergunta do usuário                         │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │ Resposta da IA — texto analítico            │   │
│  │ + badge "Contextos: Ocupação · Programação" │   │
│  │ + custo estimado (discreto, no rodapé)      │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
├─────────────────────────────────────────────────────┤
│ [Campo de texto: Faça uma pergunta...]    [Enviar]  │
└─────────────────────────────────────────────────────┘
```

**Detalhes visuais:**
- Mensagens do usuário: alinhadas à direita, fundo coral claro
- Respostas da IA: alinhadas à esquerda, fundo card padrão
- Badge de contextos: discreto, abaixo de cada resposta
- Indicador de loading: "Analisando dados..." com animação simples
- Custo estimado: `~US$ 0,08` em texto pequeno no rodapé da resposta

---

## 8. Implementação — passos (Claude Code)

1. **Instalar dependência:** `pip install anthropic` → adicionar ao `requirements.txt`
2. **Variável de ambiente:** `ANTHROPIC_API_KEY` já deve estar no Railway (verificar)
3. **`app.py`** — adicionar:
   - `POST /api/inteligencia` com lógica de contexto e chamada à API
   - Função `montar_contexto(contextos_list)` que lê os JSONs e monta o payload
   - Função `resumir_estoque()` que gera versão compacta do estoque
4. **`boaonda_inteligencia.html`** — novo dashboard com:
   - Chat UI (chips + histórico + input)
   - Seleção automática de contexto via palavras-chave
   - Streaming da resposta (opcional — melhora UX)
5. **`index.html`** — adicionar card "IA Intelligence" no organismo do portal
6. Registrar o arquivo em `MODULOS` no `index.html`

---

## 9. Custo estimado por uso

| Cenário | Tokens enviados | Custo aprox. |
|---|---|---|
| Pergunta simples (só portal.json) | ~500 tokens | US$ 0,01 |
| Pergunta de ocupação/programação | ~15.000 tokens | US$ 0,05 |
| Pergunta com vendas + faturamento | ~25.000 tokens | US$ 0,08 |
| Pergunta com estoque (resumido) | ~20.000 tokens | US$ 0,06 |
| Pergunta com tudo | ~40.000 tokens | US$ 0,15 |

Média esperada por pergunta: **US$ 0,05–0,10**. Com uso diário de 20 perguntas → ~US$ 1–2/dia → ~US$ 30–60/mês.

---

## 10. Roadmap de evolução — Fases B e C (não nesta fase)

A Fase A (este documento) entrega um chat pontual de alto valor imediato.
As fases seguintes transformam o chat num sistema que aprende e evolui com o uso.
O pré-requisito para avançar é acumular histórico real de perguntas — por isso a sequência importa.

---

### Fase B — Memória e histórico (após 2–3 meses de uso da Fase A)

**Objetivo:** o sistema lembra o que foi perguntado e acumula contexto operacional.

**O que implementar:**
- Salvar cada interação no banco MySQL (`db_mysql.py` já existe no projeto):
  tabela `inteligencia_historico` com campos: `data`, `pergunta`, `resposta`,
  `contextos_usados`, `tokens`, `custo_usd`
- Incluir as últimas 10 perguntas do histórico no contexto de cada nova pergunta
  (o modelo "sabe" o que foi discutido antes, mesmo em sessões diferentes)
- Painel de "Perguntas frequentes" na aba Inteligência, gerado automaticamente
  com base nas perguntas mais feitas nos últimos 30 dias
- Modo "Análise profunda": botão que envia todos os contextos e gera um relatório
  semanal estruturado (programação + ocupação + vendas + estoque + faturamento)
- Streaming de resposta: exibir palavra por palavra em vez de esperar o texto completo

**Gatilho para iniciar:** acumular 200+ perguntas no histórico (≈2 semanas de uso diário).

---

### Fase C — Inteligência evolutiva (após 4–6 meses de uso)

**Objetivo:** o sistema identifica padrões, antecipa problemas e calibra recomendações
para o contexto específico da Boaonda — sem precisar ser perguntado.

**O que implementar:**

1. **Padrões recorrentes identificados automaticamente**
   Um job semanal analisa o histórico de dados (ocupação, vendas, programação)
   e salva observações como: "Pool Rotativa esteve acima de 100% nas últimas 4 semanas",
   "Referência 1317 NELLIE sempre lidera o volume em semanas de alta", etc.
   Esses padrões são incluídos automaticamente no contexto de toda nova conversa.

2. **Alertas proativos**
   Na home do portal, além dos KPIs atuais, um card "IA detectou" que aparece
   quando o sistema identifica algo relevante — sem o usuário precisar perguntar.
   Exemplos: "Rotativa no mesmo padrão de sobrecarga da semana 06/Jun",
   "Faturamento previsto de agosto abaixo da média dos últimos 3 meses".

3. **Correlação decisão → resultado**
   Campo opcional nas respostas: "O gestor adotou esta recomendação?" (Sim/Não).
   Com o tempo, o sistema correlaciona quais recomendações geraram bons resultados
   e ajusta o peso das sugestões futuras para o padrão da Boaonda.

4. **Memória de contexto de negócio**
   JSON persistente `dados_memoria_negocio.json` com fatos aprendidos sobre
   a operação: sazonalidades observadas, referências estratégicas, restrições
   conhecidas (ex: "Rotativa é cronicamente o gargalo no 2º trimestre").
   Atualizado automaticamente pelo job semanal e incluído em todo prompt.

**Gatilho para iniciar:** 500+ interações históricas + 3+ meses de dados de ocupação.

---

### Resumo do roadmap

```
FASE A — Chat pontual                    ← IMPLEMENTAR AGORA
  Chat com os dados do portal
  Seleção de contexto por pergunta
  Chips de perguntas sugeridas
  Sem memória entre sessões

FASE B — Memória e histórico             ← após 2–3 meses
  Histórico salvo no MySQL
  Perguntas frequentes automáticas
  Streaming de resposta
  Análise profunda semanal

FASE C — Inteligência evolutiva          ← após 4–6 meses
  Padrões identificados automaticamente
  Alertas proativos na home
  Correlação decisão → resultado
  Memória de contexto de negócio
```
