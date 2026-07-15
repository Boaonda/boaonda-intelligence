"""
Boaonda Intelligence — Processador de dados
============================================
Lógica de processamento extraída do atualizar.py original (v1.1), refatorada
em funções reutilizáveis tanto pela CLI (atualizar.bat) quanto pela rota
/upload do app Flask.

Entradas:
    - 3YS.csv   (exportado do ERP — vendas e programação)
    - ESQT.xls  (exportado do ERP — estoque PA)

Saídas (gravadas em output_dir):
    - dados_vendas.json
    - dados_programacao.json
    - dados_refs_tabela.json
    - dados_estoque.json
    - dados_portal.json
    - boaonda_dados_completos.json
"""

import csv, json, os, sys, glob
from datetime import datetime, timedelta
from collections import defaultdict, Counter

META_SEMANAL = 30000
LOCAIS_ESTOQUE = {'156','157','158','159','160','234','30','VI','199'}
GRUPOS_OK = ['MERCADO INTERNO','ISENTO','ECOMMERCE','E-COMMERCE','EXPORTA','MOULD']

# Mapeamento de grades de pronta entrega → numeração e proporção de pares.
# Chave = LocEst (coluna "GradeDoLocalDeEstoque"/local de estoque do ESQT).
# Origem: planilha "GRADES PRONTA ENTREGA.xlsx" (abas EVA + TR). Para atualizar
# (novas grades/numerações), reenvie a planilha e este dicionário é regerado.
# 'sizes' são os rótulos de numeração (alguns são faixas, ex.: '33/34');
# 'pattern' é a quantidade de pares por numeração dentro de UMA grade completa.
GRADES_NUMERACAO = {
    '101': {'code': 'G', 'sizes': ['33/34', '35/36', '37/38', '39/40'], 'pattern': [2, 4, 4, 2]},
    '102': {'code': 'J', 'sizes': ['37/38', '39/40', '41/42', '43/44'], 'pattern': [2, 4, 4, 2]},
    '103': {'code': 'R', 'sizes': ['34', '35', '36', '37', '38', '39'], 'pattern': [1, 2, 3, 3, 2, 1]},
    '104': {'code': 'S', 'sizes': ['33/34', '35', '36', '37', '38', '39/40'], 'pattern': [1, 2, 3, 3, 2, 1]},
    '107': {'code': 'I', 'sizes': ['33/34', '35/36', '37/38', '39/40'], 'pattern': [2, 5, 4, 1]},
    '118': {'code': 'XE', 'sizes': ['38', '39', '40', '41', '42', '43'], 'pattern': [1, 2, 3, 3, 2, 1]},
    '134': {'code': 'XU', 'sizes': ['37', '38', '39', '40', '41', '42', '43'], 'pattern': [1, 1, 2, 3, 2, 2, 1]},
    '135': {'code': 'XV', 'sizes': ['35/36', '37/38'], 'pattern': [3, 3]},
    '137': {'code': 'XX', 'sizes': ['35', '36', '37', '38'], 'pattern': [1, 2, 2, 1]},
    '138': {'code': 'XZ', 'sizes': ['39/40', '41/42'], 'pattern': [3, 3]},
    '148': {'code': 'WR', 'sizes': ['35/36', '37/38', '39/40'], 'pattern': [2, 2, 2]},
    '149': {'code': 'WS', 'sizes': ['34', '35', '36', '37', '38', '39', '40'], 'pattern': [1, 2, 3, 2, 2, 1, 1]},
    '150': {'code': 'WT', 'sizes': ['39', '40', '41', '42'], 'pattern': [1, 2, 2, 1]},
    '151': {'code': 'WU', 'sizes': ['39', '40', '41', '42'], 'pattern': [1, 2, 2, 1]},
    '152': {'code': 'WV', 'sizes': ['35', '36', '37', '38'], 'pattern': [1, 2, 2, 1]},
    '153': {'code': 'F', 'sizes': ['25/26', '27/28', '29/30', '31/32'], 'pattern': [2, 4, 4, 2]},
    '154': {'code': 'WW', 'sizes': ['37', '38', '39', '40', '41', '42', '43', '44'], 'pattern': [1, 1, 2, 2, 2, 2, 1, 1]},
    '156': {'code': 'WY', 'sizes': ['40', '41', '42', '43'], 'pattern': [1, 2, 2, 1]},
    '157': {'code': 'WZ', 'sizes': ['38', '39', '40', '41', '42', '43', '44'], 'pattern': [1, 2, 2, 3, 2, 1, 1]},
    '158': {'code': 'FA', 'sizes': ['21/22', '23/24', '25/26', '27/28', '29/30', '31/32'], 'pattern': [2, 2, 2, 2, 2, 2]},
    '159': {'code': 'FB', 'sizes': ['21/22', '23/24', '25/26', '27/28', '29/30', '31/32'], 'pattern': [1, 1, 2, 2, 3, 3]},
    '192': {'code': 'BO2', 'sizes': ['35', '36', '37', '38'], 'pattern': [1, 2, 2, 1]},
    '302': {'code': 'B35X', 'sizes': ['35'], 'pattern': [6]},
    '303': {'code': 'B36X', 'sizes': ['36'], 'pattern': [6]},
    '304': {'code': 'B37X', 'sizes': ['37'], 'pattern': [6]},
    '305': {'code': 'B38X', 'sizes': ['38'], 'pattern': [6]},
    '306': {'code': 'B39/40x', 'sizes': ['39/40'], 'pattern': [6]},
    '307': {'code': 'B35/36', 'sizes': ['35/36'], 'pattern': [6]},
    '308': {'code': 'B37/38', 'sizes': ['37/38'], 'pattern': [6]},
    '309': {'code': 'B39/40', 'sizes': ['39/40'], 'pattern': [6]},
}

IDX = {
    'razao':1,'ref':7,'descr':8,'forma':9,'qtd':10,
    'dt_ent':11,'dt_fat':12,'pedido':13,'anomes':18,
    'marca':20,'linha':23,'vlr':26,'cod_esp':27,'abr_grp':29,
    'holding':16,'nomeholder':17,'plano':35,'dt_plano':77,
    'pos_item':40,'etapa':39,'local':72,'tipomontagem':85,'cfop':71,
    'conta_contabil':81,'vlr_total':86,'uf':87,'representante':88,
}

# Nome da coluna no cabeçalho do 3YS.csv para cada campo de IDX. Usado para
# localizar as colunas PELO NOME em vez de posição fixa — assim, se o T.I.
# adicionar/remover/reordenar colunas na exportação, a importação continua
# funcionando (a posição fixa em IDX vira apenas fallback). A comparação é
# case-insensitive e ignora espaços nas pontas.
CSV_COL_NAMES = {
    'razao':'razao_social', 'ref':'Referencia', 'descr':'Descricao',
    'forma':'Combinacao', 'qtd':'qtd_item', 'dt_ent':'dt_entrada',
    'dt_fat':'dt_faturam', 'pedido':'pedido', 'anomes':'anomesentrada',
    'marca':'Marca', 'linha':'Linha', 'vlr':'valorliquido',
    'cod_esp':'cod_esp_ent_sai', 'abr_grp':'descricaogrpcad',
    'holding':'holding', 'nomeholder':'nomeholding', 'plano':'planoproducao',
    'dt_plano':'dt_plano', 'pos_item':'pos_item', 'etapa':'etapa_atual',
    'local':'LocalEstoque', 'tipomontagem':'CorPalmilha', 'cfop':'cfop',
    'conta_contabil':'ContaContabil', 'vlr_total':'valortotal',
    'uf':'uf', 'representante':'representante',
}

# Query usada quando MYSQL_HOST está configurado (ver db_mysql.py) — substitui
# a leitura do 3YS.csv. Os alias das colunas seguem as chaves de IDX usadas
# pelo processamento, para que _linha_de_db_row monte a linha "sintética"
# direto a partir do dict retornado pelo MySQL.
QUERY_3YS = """
SELECT
    razao_social    AS razao,
    referencia      AS ref,
    qtdpares        AS qtd,
    dt_entrada      AS dt_ent,
    dt_faturam      AS dt_fat,
    pedido          AS pedido,
    anomesentrada   AS anomes,
    linha           AS linha,
    cod_esp_ent_sai AS cod_esp,
    abr_grp         AS abr_grp,
    nomeholding     AS nomeholder,
    planoproducao   AS plano,
    dt_plano        AS dt_plano,
    pos_item        AS pos_item,
    etapa_atual     AS etapa,
    LocalEstoque    AS local,
    CorPalmilha     AS tipomontagem,
    cfop            AS cfop,
    {col_conta}     AS conta_contabil,
    marca           AS marca,
    {col_valor}     AS vlr,
    {col_valor_total} AS vlr_total,
    {col_uf}        AS uf,
    {col_representante} AS representante,
    {col_combinacao} AS forma
FROM mould.v_entradapedidos_extended v
WHERE v.dt_entrada >= date_format(date_sub(current_date, interval 1 year), '%Y/01/01')
"""

# Volume mínimo de linhas esperado da consulta acima (1+ ano de pedidos —
# normalmente milhares). Abaixo disso, é mais provável uma falha de conexão
# com o MySQL interno do que um período real sem movimento.
MIN_LINHAS_3YS_MYSQL = 100

# Pedido em Carteira — pos_item que não devem entrar (já cancelados ou já
# totalmente faturados/entregues, não representam carteira aberta).
CARTEIRA_POS_ITEM_EXCLUIDOS = {'CANCELADO', 'FATURADO'}

# Mapeamento abr_grp -> canal de vendas (MI/ME/ECOM).
# Apenas calçados — EVA, SOLA, CLIENTES NACIONAIS e GRUPO MOULD são outras
# unidades de negócio e ficam fora desta análise (Mould mantém seu próprio
# total à parte, ver classifica_venda).
VENDA_CANAL_POR_GRUPO = {
    'CALCADO - MERCADO INTERNO': 'MI',
    'CALCADO - CLIENTES ISENTOS': 'MI',
    'EXPORTACAO - CALCADOS': 'ME',
    'E-COMMERCE': 'ECOM',
}

# cod_esp_ent_sai -> tipo de pedido dentro do canal:
#   1  = Programado
#   10 = Venda Equiparada (exportação)
#   22 = Pronta Entrega
#   31 = Venda Mista (programação + pronta entrega no mesmo pedido)
#   32 = Ecommerce
VENDA_TIPO_POR_COD = {'1':'PROG','10':'EQUIPARADA','22':'PE','31':'MISTA','32':'ECOM'}

# Espécies de orçamento de Mercado Externo — nunca serão faturadas (pedidos
# não aprovados). Excluídas do faturamento previsto, CFOP, conta contábil e
# pendências retroativas.
ESPECIES_ORCAMENTO_ME = {'27', '33'}

# ─── HELPERS ───────────────────────────────────────────────────────────
def achar_3ys(diretorio='.'):
    """Detecta arquivo 3YS automaticamente — qualquer variação de nome."""
    for arq in os.listdir(diretorio):
        if arq.upper().startswith('3YS') and (arq.upper().endswith('.CSV') or arq == '3YS'):
            return os.path.join(diretorio, arq)
    return os.path.join(diretorio, '3YS.csv')  # fallback para mensagem de erro clara

def parse_date(s):
    if not s: return None
    s = s.strip()
    for fmt in ['%d/%m/%Y','%d/%m/%y','%Y-%m-%d']:
        try: return datetime.strptime(s, fmt)
        except: pass
    return None

def semana_label(monday, friday):
    m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    return f"{monday.day:02d}/{m[monday.month-1]}-{friday.day:02d}/{m[friday.month-1]}"

def mes_label_str(dt):
    m = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
         'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
    return f"{m[dt.month-1]}/{dt.year}"

def mes_label_curto(dt):
    m = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    return f"{m[dt.month-1]}/{dt.year}"

def g(row, idx):
    try: return row[idx].strip()
    except: return ''

def corrigir_mojibake(s):
    """Corrige texto que veio do 3YS com acentos duplamente
    mal-codificados (ex: 'AVALIAÃ‡ÃƒO' -> 'AVALIAÇÃO',
    'NÃ£o se aplica' -> 'Não se aplica')."""
    if not s: return s
    for _ in range(4):
        try:
            s2 = s.encode('cp1252').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            return s
        if s2 == s: return s
        s = s2
    return s

MARCA_COMPOSTO_EVA = 'COMPOSTOS EVA'

def is_composto_eva(marca):
    """Composto de EVA é um mercado separado de calçados, identificado pela
    coluna MARCA — sem filtro de cliente cadastrado ou espécie de venda
    (abr_grp/cod_esp), a pedido do usuário: toda linha com essa marca conta."""
    return (marca or '').strip().upper() == MARCA_COMPOSTO_EVA

def classifica_canal(cod, abr):
    if cod == '1': return 'MI'
    if cod == '17': return 'ME'
    if cod in ('22','31'): return 'PE'
    if cod == '32' or 'ECOMMERCE' in abr or 'E-COMMERCE' in abr: return 'ECOM'
    if 'MERCADO INTERNO' in abr or 'ISENTO' in abr: return 'MI'
    if 'EXPORTA' in abr: return 'ME'
    if 'MOULD' in abr: return 'GRUPO_MOULD'
    return None

def classifica_venda(abr, cod, pos_item):
    """Classifica uma linha de venda em (canal, tipo) para o relatório de
    Vendas (MI/ME/ECOM + tipo de pedido).

    Regras:
      - Itens 'Cancelado' (pos_item) nunca contam no volume de vendas.
      - Canal vem do abr_grp (CALCADO - MERCADO INTERNO/CLIENTES ISENTOS -> MI,
        EXPORTACAO - CALCADOS -> ME, E-COMMERCE -> ECOM).
      - Tipo vem do cod_esp_ent_sai (1=Programado, 10=Venda Equiparada,
        22=Pronta Entrega, 31=Venda Mista, 32=Ecommerce).
      - Linhas de GRUPO MOULD entram à parte, em 'GRUPO_MOULD' (sem tipo),
        para manter o total dessa unidade de negócio visível.
    """
    if pos_item.strip().upper() == 'CANCELADO':
        return None, None
    canal = VENDA_CANAL_POR_GRUPO.get(abr)
    tipo = VENDA_TIPO_POR_COD.get(cod)
    if canal and tipo:
        return canal, tipo
    if 'MOULD' in abr:
        return 'GRUPO_MOULD', None
    return None, None

def detectar_sep(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        sample = f.read(2000)
    return ';' if sample.count(';') > sample.count(',') else ','

def _db_val_to_str(v):
    """Converte um valor vindo do MySQL (Decimal/date/int/None/...) para o
    mesmo formato de string que vinha de uma célula do 3YS.csv."""
    if v is None: return ''
    if hasattr(v, 'strftime'): return v.strftime('%Y-%m-%d')
    return str(v)

def _linha_de_db_row(db_row):
    """Monta uma linha 'sintética' (lista indexável por IDX) a partir de um
    dict retornado pelo MySQL (consulta QUERY_3YS)."""
    linha = [''] * (max(IDX.values()) + 1)
    for chave, idx in IDX.items():
        linha[idx] = _db_val_to_str(db_row.get(chave))
    return linha

def carregar_linhas_3ys(arquivo_3ys=None):
    """Retorna a lista de linhas (cada uma indexável por IDX) a processar.

    Se MYSQL_HOST estiver configurado (rede interna), busca direto do MySQL
    via QUERY_3YS. Caso contrário, lê do arquivo 3YS.csv informado."""
    if os.environ.get('MYSQL_HOST'):
        import db_mysql
        print("    Lendo 3YS do MySQL...")
        col_valor = db_mysql.achar_coluna_valor_liquido()
        if col_valor:
            col_valor_sql = f'v.`{col_valor}`'
        else:
            print("    AVISO: coluna 'valor líquido' não encontrada na view "
                  "— vendas em R$ ficarão zeradas")
            col_valor_sql = '0'
        col_conta = db_mysql.achar_coluna_conta_contabil()
        if col_conta:
            col_conta_sql = f'v.`{col_conta}`'
        else:
            print("    AVISO: coluna 'conta contábil' não encontrada na view "
                  "— faturamento por conta contábil ficará vazio")
            col_conta_sql = "''"
        # Valor total (bruto: com IPI/frete) — usado no faturamento p/ bater com
        # a contabilidade. Se a view não tiver, o faturamento cai para o líquido.
        col_valor_total = db_mysql.achar_coluna_valor_total()
        if col_valor_total:
            col_valor_total_sql = f'v.`{col_valor_total}`'
        else:
            print("    AVISO: coluna 'valor total' (bruto) não encontrada na view "
                  "— faturamento usará o valor líquido")
            col_valor_total_sql = '0'
        # UF e Representante — usados nos filtros de Análise da Carteira. Se a
        # view não tiver, os filtros ficam vazios (sem quebrar o resto).
        col_uf = db_mysql.achar_coluna_uf()
        col_uf_sql = f'v.`{col_uf}`' if col_uf else "''"
        col_representante = db_mysql.achar_coluna_representante()
        col_representante_sql = f'v.`{col_representante}`' if col_representante else "''"
        col_combinacao = db_mysql.achar_coluna_combinacao()
        col_combinacao_sql = f'v.`{col_combinacao}`' if col_combinacao else "''"
        db_rows = db_mysql.consultar(QUERY_3YS.format(
            col_valor=col_valor_sql, col_conta=col_conta_sql,
            col_valor_total=col_valor_total_sql,
            col_uf=col_uf_sql, col_representante=col_representante_sql,
            col_combinacao=col_combinacao_sql))
        print(f"    {len(db_rows):,} linhas carregadas do MySQL")
        if len(db_rows) < MIN_LINHAS_3YS_MYSQL:
            raise RuntimeError(
                f"Consulta ao MySQL retornou apenas {len(db_rows)} linha(s) "
                f"(esperado pelo menos {MIN_LINHAS_3YS_MYSQL}). Abortando para não "
                "sobrescrever vendas/programação/carteira com dados zerados — "
                "provável instabilidade de conexão com o banco interno."
            )
        return [_linha_de_db_row(r) for r in db_rows]

    sep = detectar_sep(arquivo_3ys)
    linhas_raw = []
    header = []
    with open(arquivo_3ys, 'r', encoding='utf-8', errors='replace') as f_:
        reader = csv.reader(f_, delimiter=sep)
        header = next(reader, [])
        for row in reader:
            linhas_raw.append(row)

    print(f"    CSV: {len(linhas_raw):,} linhas, {len(header)} colunas, sep={sep!r}")
    # Reordena cada linha para o layout do IDX resolvendo as colunas PELO NOME
    # do cabeçalho (robusto a inserção/remoção/reordenação de colunas pelo T.I.).
    linhas = _remapear_csv_por_nome(header, linhas_raw)
    return linhas


def _remapear_csv_por_nome(header, linhas_raw):
    """Constrói linhas 'sintéticas' (indexáveis por IDX) a partir do CSV,
    localizando cada coluna pelo NOME do cabeçalho (CSV_COL_NAMES). Se um nome
    não for achado, cai no índice fixo de IDX como fallback e avisa no log."""
    hlow = {h.strip().lower(): i for i, h in enumerate(header)}
    origem = {}   # campo -> índice real no CSV
    usou_fallback = []
    for campo, idx_fixo in IDX.items():
        nome = CSV_COL_NAMES.get(campo)
        pos = hlow.get(nome.strip().lower()) if nome else None
        if pos is None:
            pos = idx_fixo          # fallback: posição fixa histórica
            usou_fallback.append(campo)
        origem[campo] = pos

    # Log de diagnóstico — mostra de onde cada campo-chave foi lido
    campos_chave = ('abr_grp', 'anomes', 'qtd', 'pos_item', 'cod_esp', 'local', 'plano')
    for c in campos_chave:
        pos = origem[c]
        hdr = header[pos] if pos < len(header) else '(fora)'
        val = g(linhas_raw[0], pos) if linhas_raw else '—'
        marca = ' [POS.FIXA - nome nao encontrado!]' if c in usou_fallback else ''
        print(f"      {c:12s} <- col[{pos:2d}] {hdr!r:24s} ex={val!r}{marca}")
    if usou_fallback:
        print(f"    *** AVISO: campos sem coluna no CSV (usando posição fixa): "
              f"{', '.join(usou_fallback)} ***")

    max_idx = max(IDX.values())
    linhas = []
    for row in linhas_raw:
        nova = [''] * (max_idx + 1)
        for campo, pos in origem.items():
            if pos < len(row):
                nova[IDX[campo]] = row[pos]
        linhas.append(nova)
    return linhas


def diagnostico_3ys():
    """Diagnóstico da fonte de Vendas/Carteira/Programação. Conta linhas e
    mede o preenchimento dos campos-chave — usado para investigar dashboards
    zerados (ex.: view do MySQL retornando linhas com campos vazios). Não
    aplica a trava de mínimo: reporta o que vier, mesmo vazio."""
    info = {'mysql_host_configurado': bool(os.environ.get('MYSQL_HOST')), 'total_linhas': 0}
    if not os.environ.get('MYSQL_HOST'):
        info['fonte'] = 'arquivo CSV (MYSQL_HOST não está configurado)'
        return info
    import db_mysql
    info['fonte'] = 'MySQL (v_entradapedidos_extended)'
    col_valor = db_mysql.achar_coluna_valor_liquido()
    info['coluna_valor_liquido'] = col_valor or '(NÃO encontrada na view)'
    col_valor_sql = f'v.`{col_valor}`' if col_valor else '0'
    col_valor_total = db_mysql.achar_coluna_valor_total()
    info['coluna_valor_total'] = col_valor_total or '(NÃO encontrada na view)'
    col_valor_total_sql = f'v.`{col_valor_total}`' if col_valor_total else '0'
    col_conta = db_mysql.achar_coluna_conta_contabil()
    col_conta_sql = f'v.`{col_conta}`' if col_conta else "''"
    col_uf = db_mysql.achar_coluna_uf()
    info['coluna_uf'] = col_uf or '(NÃO encontrada na view)'
    col_uf_sql = f'v.`{col_uf}`' if col_uf else "''"
    col_representante = db_mysql.achar_coluna_representante()
    info['coluna_representante'] = col_representante or '(NÃO encontrada na view)'
    col_representante_sql = f'v.`{col_representante}`' if col_representante else "''"
    col_combinacao = db_mysql.achar_coluna_combinacao()
    info['coluna_combinacao'] = col_combinacao or '(NÃO encontrada na view)'
    col_combinacao_sql = f'v.`{col_combinacao}`' if col_combinacao else "''"
    db_rows = db_mysql.consultar(QUERY_3YS.format(
        col_valor=col_valor_sql, col_conta=col_conta_sql,
        col_valor_total=col_valor_total_sql,
        col_uf=col_uf_sql, col_representante=col_representante_sql,
        col_combinacao=col_combinacao_sql))
    info['total_linhas'] = len(db_rows)
    linhas = [_linha_de_db_row(r) for r in db_rows]
    campos = ('qtd', 'pos_item', 'abr_grp', 'cod_esp', 'anomes', 'marca', 'ref', 'vlr')
    preench = {c: 0 for c in campos}
    pos_vals, abr_vals = Counter(), Counter()
    for row in linhas:
        for c in campos:
            if g(row, IDX[c]).strip():
                preench[c] += 1
        p = g(row, IDX['pos_item']).strip().upper()
        if p: pos_vals[p] += 1
        a = g(row, IDX['abr_grp']).strip().upper()
        if a: abr_vals[a] += 1
    info['campos_preenchidos'] = preench
    info['pos_item_valores'] = pos_vals.most_common(12)
    info['abr_grp_valores'] = abr_vals.most_common(12)
    if linhas:
        info['amostra_1a_linha'] = {c: g(linhas[0], IDX[c]) for c in campos}
    return info

# ─── VENDAS ──────────────────────────────────────────────────────────
def processar_vendas(linhas, mes_atual, output_dir='.'):
    print("\n  Processando vendas...")
    canais_mes = {'MI':0,'ME':0,'ECOM':0,'GRUPO_MOULD':0}
    canais_total = {'MI':0,'ME':0,'ECOM':0,'GRUPO_MOULD':0}
    canais_mes_valor = {'MI':0.0,'ME':0.0,'ECOM':0.0,'GRUPO_MOULD':0.0}
    canais_total_valor = {'MI':0.0,'ME':0.0,'ECOM':0.0,'GRUPO_MOULD':0.0}
    tipos_mes = {'MI':defaultdict(int),'ME':defaultdict(int),'ECOM':defaultdict(int)}
    mensal = defaultdict(lambda: {'MI':defaultdict(int),'ME':defaultdict(int),
                                   'ECOM':defaultdict(int),'GRUPO_MOULD':0})
    mensal_valor = defaultdict(lambda: {'MI':defaultdict(float),'ME':defaultdict(float),
                                         'ECOM':defaultdict(float),'GRUPO_MOULD':0.0})
    linha_canal = defaultdict(lambda: defaultdict(int))
    refs = Counter()
    refs_mi = Counter()
    refs_me = Counter()
    refs_ec = Counter()
    refs_valor = Counter()
    refs_mi_valor = Counter()
    refs_me_valor = Counter()
    refs_ec_valor = Counter()
    holdings = defaultdict(lambda: defaultdict(int))

    for row in linhas:
        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        pos_item = g(row, IDX['pos_item'])
        canal, tipo = classifica_venda(abr, cod, pos_item)
        if not canal: continue
        try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
        except: qtd = 0
        if qtd <= 0: continue
        try: valor = float(g(row, IDX['vlr']).replace(',','.'))
        except: valor = 0.0
        anomes = g(row, IDX['anomes'])
        ref = g(row, IDX['ref'])
        linha = g(row, IDX['linha'])
        ln = linha if linha in ('CLASSIC','EVA','WORKS','FIT','DAY BY DAY') else 'OUTROS'
        holding = g(row, IDX['nomeholder']) or g(row, IDX['razao'])[:40]

        canais_total[canal] += qtd
        canais_total_valor[canal] += valor
        if anomes == mes_atual:
            canais_mes[canal] += qtd
            canais_mes_valor[canal] += valor
            if tipo: tipos_mes[canal][tipo] += qtd
        if anomes and len(anomes) == 6 and anomes.isdigit():
            if tipo:
                mensal[anomes][canal][tipo] += qtd
                mensal_valor[anomes][canal][tipo] += valor
            else:
                mensal[anomes][canal] += qtd
                mensal_valor[anomes][canal] += valor
        if ln != 'OUTROS':
            linha_canal[ln][canal] += qtd
        if ref:
            refs[ref] += qtd
            refs_valor[ref] += valor
            if canal == 'MI':
                refs_mi[ref] += qtd; refs_mi_valor[ref] += valor
            elif canal == 'ME':
                refs_me[ref] += qtd; refs_me_valor[ref] += valor
            elif canal == 'ECOM':
                refs_ec[ref] += qtd; refs_ec_valor[ref] += valor
        holdings[holding][canal] += qtd

    # Volume "calçados" (MI+ME+ECOM) — Grupo Mould é outra unidade de negócio
    # e fica de fora deste total, mas continua disponível em canais_mes/total.
    total_mes = canais_mes['MI'] + canais_mes['ME'] + canais_mes['ECOM']
    print(f"    Mês {mes_atual}: {total_mes:,} pares (MI+ME+ECOM) "
          f"| Grupo Mould: {canais_mes['GRUPO_MOULD']:,}")
    # Diagnóstico — mostra os anomes presentes para detectar incompatibilidade de formato
    top_anomes = sorted(mensal.keys())[-6:]
    print(f"    anomes no CSV (últimos 6): {top_anomes} | esperado: {mes_atual}")
    if mes_atual not in mensal:
        print(f"    *** AVISO: {mes_atual} NÃO encontrado nos dados — verifique formato do campo anomes no CSV ***")

    mensal_out = {}
    for k, v in sorted(mensal.items()):
        mensal_out[k] = {
            'MI': dict(v['MI']), 'ME': dict(v['ME']), 'ECOM': dict(v['ECOM']),
            'GRUPO_MOULD': v['GRUPO_MOULD'],
        }
    tipos_mes_out = {c: dict(t) for c, t in tipos_mes.items()}

    total_mes_valor = round(canais_mes_valor['MI'] + canais_mes_valor['ME'] + canais_mes_valor['ECOM'], 2)

    mensal_valor_out = {}
    for k, v in sorted(mensal_valor.items()):
        mensal_valor_out[k] = {
            'MI': {t: round(x,2) for t,x in v['MI'].items()},
            'ME': {t: round(x,2) for t,x in v['ME'].items()},
            'ECOM': {t: round(x,2) for t,x in v['ECOM'].items()},
            'GRUPO_MOULD': round(v['GRUPO_MOULD'],2),
        }
    canais_mes_valor = {c: round(x,2) for c,x in canais_mes_valor.items()}
    canais_total_valor = {c: round(x,2) for c,x in canais_total_valor.items()}

    # Gravar dados_vendas.json
    dados_vend = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'mes_atual': mes_atual,
        'canais_mes': canais_mes,
        'tipos_mes': tipos_mes_out,
        'canais_total': canais_total,
        'total_mes': total_mes,
        'mensal': mensal_out,
        'canais_mes_valor': canais_mes_valor,
        'canais_total_valor': canais_total_valor,
        'total_mes_valor': total_mes_valor,
        'mensal_valor': mensal_valor_out,
        'refs_top20': {
            'MI': refs_mi.most_common(20) if hasattr(refs_mi,'most_common') else [],
            'ME': refs_me.most_common(20) if hasattr(refs_me,'most_common') else [],
            'EC': refs_ec.most_common(20) if hasattr(refs_ec,'most_common') else [],
        },
        'refs_top20_valor': {
            'MI': [(r, round(v,2)) for r,v in refs_mi_valor.most_common(20)],
            'ME': [(r, round(v,2)) for r,v in refs_me_valor.most_common(20)],
            'EC': [(r, round(v,2)) for r,v in refs_ec_valor.most_common(20)],
        },
        'pendente_validacao': canais_mes.get('MI',0) < 10000,
    }
    with open(os.path.join(output_dir, 'dados_vendas.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados_vend, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_vendas.json gerado")

    return {
        'canais_mes': canais_mes, 'tipos_mes': tipos_mes_out, 'canais_total': canais_total,
        'total_mes': total_mes,
        'mensal': mensal_out,
        'linha_canal': {k: dict(v) for k,v in linha_canal.items()},
        'refs_top20': refs.most_common(20),
        'holdings_top20': [(n, dict(cv), sum(cv.values()))
            for n, cv in sorted(holdings.items(), key=lambda x: -sum(x[1].values()))[:20]],
        'pendente_validacao': canais_mes.get('MI',0) < 10000,
        '_diag': {
            'anomes_presentes': sorted(mensal_out.keys())[-6:],
            'mes_atual_ok': mes_atual in mensal_out,
        },
    }


def _holding_de(row):
    """Nome do cliente para agrupamento — holding se cadastrada, senão a
    própria razão social vira um grupo de 1 (mesmo padrão já usado no
    faturamento: nomeholding OR razao_social)."""
    return corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:60]


def gerar_dados_vendas_clientes(linhas, output_dir='.'):
    """Detalhe linha-a-linha de vendas por holding/cliente, do ano-calendário
    corrente (1º de janeiro até hoje). Alimenta o quadro 'Clientes com
    compra' (drilldown holding → referência → linha) e os cards 'Vendas do
    dia/dia anterior' do dashboard de Vendas — ambos calculados no navegador
    filtrando este arquivo por data, sem chamada ao servidor. Também alimenta
    o quadro 'Mix de produto' (referência → linha/cor), que reaproveita os
    mesmos filtros de canal/tipo/período já usados em 'Clientes com compra'.

    Estrutura: {"campos": [...], "holdings": {holding: {ref: [[...]]}}}
    Cada linha é um array compacto (não dict) para reduzir tamanho — a
    ordem dos campos está em "campos". `linha` é a linha de produto
    normalizada (CLASSIC/EVA/WORKS/FIT/DAY BY DAY/OUTROS, mesma regra do
    resumo de vendas); `cor` vem da coluna Combinacao do 3YS (ex.:
    "001 (PRETO/GRAFITE)").
    """
    print("\n  Gerando dados_vendas_clientes.json...")
    ano_atual = datetime.now().strftime('%Y')
    ano_ini = f'{ano_atual}01'
    holdings = defaultdict(lambda: defaultdict(list))
    n = 0
    for row in linhas:
        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        pos_item = g(row, IDX['pos_item'])
        canal, tipo = classifica_venda(abr, cod, pos_item)
        if canal not in ('MI', 'ME', 'ECOM'): continue
        anomes = g(row, IDX['anomes'])
        if not anomes or anomes < ano_ini: continue
        try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
        except: qtd = 0
        if qtd <= 0: continue
        try: valor = round(float(g(row, IDX['vlr']).replace(',','.')), 2)
        except: valor = 0.0
        holding = _holding_de(row)
        ref = g(row, IDX['ref']).strip() or '(sem referência)'
        dt_ent = g(row, IDX['dt_ent'])
        pedido = g(row, IDX['pedido'])
        linha_prod = g(row, IDX['linha'])
        linha_norm = linha_prod if linha_prod in ('CLASSIC','EVA','WORKS','FIT','DAY BY DAY') else 'OUTROS'
        cor = corrigir_mojibake(g(row, IDX['forma'])) or '(sem cor)'
        holdings[holding][ref].append([pedido, dt_ent, canal, tipo, qtd, valor, linha_norm, cor])
        n += 1

    saida = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'periodo_desde': f'01/01/{ano_atual}',
        'campos': ['pedido', 'dt_ent', 'canal', 'tipo', 'qtd', 'valor', 'linha', 'cor'],
        'holdings': {h: dict(refs) for h, refs in holdings.items()},
    }
    with open(os.path.join(output_dir, 'dados_vendas_clientes.json'), 'w', encoding='utf-8') as f_:
        json.dump(saida, f_, ensure_ascii=False, separators=(',', ':'))
    print(f"    ✓ dados_vendas_clientes.json gerado ({n:,} linhas, "
          f"{len(holdings):,} holdings, desde 01/01/{ano_atual})")


def gerar_dados_vendas_carteira(linhas, output_dir='.'):
    """Histórico mensal de vendas por holding/cliente (todo o histórico
    disponível no 3YS, não só o ano corrente) — granularidade de mês, sem
    detalhe de referência/linha. Alimenta a página 'Análise da Carteira'
    (novos clientes, clientes que pararam de comprar, concentração/Pareto,
    recência), que precisa comparar períodos passados (ex.: 1ª compra em
    qualquer mês do histórico) — por isso não fica restrito ao ano corrente
    como o dados_vendas_clientes.json.

    Estrutura: {"holdings": {holding: {mes: {"MI_PROG":[pares,valor], ...}}},
    "meta": {holding: {"uf": "RS", "representantes": {"MI": "Fulano", ...}}}}
    — meta.uf é a UF mais frequente do holding; meta.representantes traz o
    representante mais frequente POR CANAL (o mesmo holding pode ter reps
    diferentes por canal, ex.: e-commerce sempre cai no rep "ECOMMERCE").
    Alimenta os filtros de UF e Representante da Análise da Carteira.
    """
    print("\n  Gerando dados_vendas_carteira.json...")
    holdings = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0.0])))
    meses_presentes = set()
    holding_uf = defaultdict(Counter)
    holding_rep = defaultdict(lambda: defaultdict(Counter))
    for row in linhas:
        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        pos_item = g(row, IDX['pos_item'])
        canal, tipo = classifica_venda(abr, cod, pos_item)
        if canal not in ('MI', 'ME', 'ECOM'): continue
        anomes = g(row, IDX['anomes'])
        if not anomes or len(anomes) != 6 or not anomes.isdigit(): continue
        try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
        except: qtd = 0
        if qtd <= 0: continue
        try: valor = float(g(row, IDX['vlr']).replace(',','.'))
        except: valor = 0.0
        holding = _holding_de(row)
        chave = f'{canal}_{tipo}'
        d = holdings[holding][anomes][chave]
        d[0] += qtd; d[1] += valor
        meses_presentes.add(anomes)
        uf_val = g(row, IDX['uf']).strip()
        rep_val = g(row, IDX['representante']).strip()
        if uf_val: holding_uf[holding][uf_val] += 1
        if rep_val: holding_rep[holding][canal][rep_val] += 1

    saida_holdings = {}
    for h, meses in holdings.items():
        saida_holdings[h] = {
            m: {k: [q, round(v, 2)] for k, (q, v) in tipos.items()}
            for m, tipos in meses.items()
        }
    meta = {}
    for h in holdings:
        uf_top = holding_uf[h].most_common(1)
        reps = {c: cnt.most_common(1)[0][0] for c, cnt in holding_rep[h].items() if cnt}
        meta[h] = {'uf': uf_top[0][0] if uf_top else '', 'representantes': reps}
    saida = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'meses_disponiveis': sorted(meses_presentes),
        'holdings': saida_holdings,
        'meta': meta,
    }
    with open(os.path.join(output_dir, 'dados_vendas_carteira.json'), 'w', encoding='utf-8') as f_:
        json.dump(saida, f_, ensure_ascii=False, separators=(',', ':'))
    print(f"    ✓ dados_vendas_carteira.json gerado ({len(holdings):,} holdings, "
          f"{len(meses_presentes)} meses de histórico)")


# ─── VENDAS COMPOSTO EVA ─────────────────────────────────────────────
def processar_vendas_eva(linhas, mes_atual, output_dir='.'):
    """Vendas de Composto de EVA — mercado separado de calçados (matéria-prima
    vendida a terceiros), identificado pela coluna MARCA. Sem filtro de
    cliente cadastrado ou espécie de venda: toda linha com
    marca=='COMPOSTOS EVA' conta. Quantidade em KG (não pares)."""
    print("\n  Processando vendas Composto EVA...")
    total_mes_kg = total_mes_valor = 0.0
    mensal_kg = defaultdict(float)
    mensal_valor = defaultdict(float)
    mensal_clientes = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # mes -> cliente -> [kg, valor]
    # mensal_pedidos[mes][cliente][pedido] = [kg, valor] — drilldown de pedidos
    # dentro do cliente, mesma ideia do drilldown EVA do Faturamento.
    mensal_pedidos = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0])))
    clientes_kg = Counter()
    clientes_valor = Counter()
    refs_kg = Counter()
    refs_valor = Counter()

    for row in linhas:
        if not is_composto_eva(g(row, IDX['marca'])):
            continue
        if g(row, IDX['pos_item']).strip().upper() == 'CANCELADO':
            continue
        try: qtd_kg = float(g(row, IDX['qtd']).replace(',', '.'))
        except: qtd_kg = 0.0
        if qtd_kg <= 0: continue
        try: valor = float(g(row, IDX['vlr']).replace(',', '.'))
        except: valor = 0.0

        anomes = g(row, IDX['anomes'])
        ref = g(row, IDX['ref'])
        cliente = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:40]
        pedido = g(row, IDX['pedido']).strip() or '(sem pedido)'

        if anomes == mes_atual:
            total_mes_kg += qtd_kg
            total_mes_valor += valor
        if anomes and len(anomes) == 6 and anomes.isdigit():
            mensal_kg[anomes] += qtd_kg
            mensal_valor[anomes] += valor
            if cliente:
                mc = mensal_clientes[anomes][cliente]
                mc[0] += qtd_kg; mc[1] += valor
                mp = mensal_pedidos[anomes][cliente][pedido]
                mp[0] += qtd_kg; mp[1] += valor
        if ref:
            refs_kg[ref] += qtd_kg
            refs_valor[ref] += valor
        if cliente:
            clientes_kg[cliente] += qtd_kg
            clientes_valor[cliente] += valor

    print(f"    Mês {mes_atual}: {total_mes_kg:,.1f} kg "
          f"({len(refs_kg)} materiais, {len(clientes_kg)} clientes)")

    dados_eva = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'mes_atual': mes_atual,
        'total_mes_kg': round(total_mes_kg, 1),
        'total_mes_valor': round(total_mes_valor, 2),
        'mensal_kg': {k: round(v, 1) for k, v in sorted(mensal_kg.items())},
        'mensal_valor': {k: round(v, 2) for k, v in sorted(mensal_valor.items())},
        'mensal_clientes': {
            mes: sorted(
                [{'cliente': c, 'kg': round(v[0], 1), 'valor': round(v[1], 2),
                  'pedidos': sorted(
                      [{'pedido': p, 'kg': round(pv[0], 1), 'valor': round(pv[1], 2)}
                       for p, pv in mensal_pedidos[mes][c].items()],
                      key=lambda x: -x['kg']
                  )}
                 for c, v in clientes.items()],
                key=lambda x: -x['kg']
            )
            for mes, clientes in mensal_clientes.items()
        },
        'clientes_top20_kg': [(c, round(v, 1)) for c, v in clientes_kg.most_common(20)],
        'clientes_top20_valor': [(c, round(v, 2)) for c, v in clientes_valor.most_common(20)],
        'refs_top20_kg': [(r, round(v, 1)) for r, v in refs_kg.most_common(20)],
        'refs_top20_valor': [(r, round(v, 2)) for r, v in refs_valor.most_common(20)],
    }
    with open(os.path.join(output_dir, 'dados_vendas_eva.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados_eva, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_vendas_eva.json gerado")
    return dados_eva

# ─── PROGRAMAÇÃO ─────────────────────────────────────────────────────
def processar_programacao(linhas, output_dir='.'):
    print("\n  Processando programação...")

    refs_por_semana = defaultdict(Counter)
    refs_por_mes    = defaultdict(Counter)
    sem_labels_rp   = {}
    mes_labels_rp   = {}
    semanas = defaultdict(lambda: {
        'CLASSIC':0,'EVA':0,'WORKS':0,'FIT':0,'DAY BY DAY':0,'OUTROS':0,
        'total':0,'export':0,'pe':0,'mi':0,'convencional':0,'montado':0,'label':'',
        'pedidos':set(),'clientes':set(),'refs':Counter()
    })
    meses = defaultdict(lambda: {
        'CLASSIC':0,'EVA':0,'WORKS':0,'FIT':0,'DAY BY DAY':0,'OUTROS':0,
        'total':0,'export':0,'pe':0,'mi':0,'convencional':0,'montado':0,
        'label':'','semanas_keys':set(),'pedidos':set(),'refs':Counter()
    })
    refs_prog = Counter()
    linhas_proc = 0

    # Detalhe item-a-item (drilldown por semana) — cobre a partir do 1º dia
    # do mês anterior ao atual, evitando exportar o histórico inteiro.
    hoje = datetime.now()
    if hoje.month == 1:
        cutoff_detalhe = datetime(hoje.year - 1, 12, 1)
    else:
        cutoff_detalhe = datetime(hoje.year, hoje.month - 1, 1)
    detalhe_raw = defaultdict(lambda: defaultdict(int))

    for row in linhas:
        abr = g(row, IDX['abr_grp']).upper()
        if not any(p in abr for p in GRUPOS_OK): continue
        plano = g(row, IDX['plano'])
        if not plano or plano in ('Não se aplica','NÃ£o se aplica',''): continue
        # NÃO excluir pos_item='Cancelado' aqui: ~94% desses itens são GRUPO
        # MOULD (armazém / pronta entrega) — produção real de reposição de
        # estoque, deliberadamente contada como volume 'pe' na programação.
        # O "Cancelado" nesses casos é status interno do armazém, não
        # cancelamento de cliente. (Ver auditoria 2026-07-10.)
        dt = parse_date(g(row, IDX['dt_plano']))
        if not dt: continue
        if dt.weekday() > 4: continue  # excluir fins de semana
        try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
        except: qtd = 0
        if qtd <= 0: continue

        linha = g(row, IDX['linha'])
        ln = linha if linha in ('CLASSIC','EVA','WORKS','FIT','DAY BY DAY') else 'OUTROS'
        razao = g(row, IDX['razao']).upper().strip()
        pedido = g(row, IDX['pedido'])
        ref = g(row, IDX['ref'])
        is_export = 'EXPORTA' in abr
        is_pe     = 'MOULD' in abr and 'ARMAZEM PRONTA ENTREGA' in razao
        is_mi     = ('MERCADO INTERNO' in abr or 'ISENTO' in abr) and not is_export and not is_pe
        # Tipo de montagem
        tp = g(row, IDX['tipomontagem']).upper()
        is_conv = 'CONVENCIONAL' in tp
        is_mont = 'MONTADO' in tp and 'CONVENCIONAL' not in tp

        monday = dt - timedelta(days=dt.weekday())
        friday = monday + timedelta(days=4)
        sem_key = monday.strftime('%Y-%m-%d')
        # O mês "dono" da semana é o mês da sexta-feira (último dia útil).
        # Assim, semanas que viram o mês (ex: seg 29/Jun-sex 03/Jul) contam
        # inteiramente para o mês da sexta-feira (Jul), e não são divididas.
        mes_key = friday.strftime('%Y-%m')

        s = semanas[sem_key]
        s[ln] += qtd; s['total'] += qtd
        s['pedidos'].add(pedido); s['clientes'].add(g(row, IDX['razao'])[:40])
        s['refs'][ref] += qtd
        if not s['label']: s['label'] = semana_label(monday, friday)
        s['mes_ref'] = mes_key
        if is_export: s['export'] += qtd
        if is_pe:     s['pe'] += qtd
        if is_mi:     s['mi'] += qtd
        if is_conv: s['convencional'] += qtd
        if is_mont: s['montado'] += qtd

        m2 = meses[mes_key]
        m2[ln] += qtd; m2['total'] += qtd
        m2['pedidos'].add(pedido); m2['refs'][ref] += qtd
        m2['semanas_keys'].add(sem_key)
        if not m2['label']: m2['label'] = mes_label_str(friday)
        if is_export: m2['export'] += qtd
        if is_pe:     m2['pe'] += qtd
        if is_mi:     m2['mi'] += qtd
        if is_conv: m2['convencional'] += qtd
        if is_mont: m2['montado'] += qtd

        refs_prog[ref] += qtd
        refs_por_semana[sem_key][ref] += qtd
        refs_por_mes[mes_key][ref]    += qtd

        if dt >= cutoff_detalhe:
            cliente_d = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:40]
            dt_plano = parse_date(g(row, IDX['dt_plano']))
            chave = (cliente_d, pedido, ref, plano, dt_plano.strftime('%d/%m/%Y') if dt_plano else '')
            detalhe_raw[sem_key][chave] += qtd
        if sem_key not in sem_labels_rp: sem_labels_rp[sem_key] = s['label']
        if mes_key not in mes_labels_rp: mes_labels_rp[mes_key] = m2['label']
        linhas_proc += 1

    print(f"    Linhas processadas: {linhas_proc:,}")
    print(f"    Semanas mapeadas:   {len(semanas)}")

    # Serializar semanas
    semanas_out = {}
    for k in sorted(semanas):
        s = semanas[k]
        semanas_out[k] = {
            'CLASSIC':s['CLASSIC'],'EVA':s['EVA'],'WORKS':s['WORKS'],
            'FIT':s['FIT'],'DAY BY DAY':s['DAY BY DAY'],'OUTROS':s['OUTROS'],
            'total':s['total'],'label':s['label'],'mes_ref':s['mes_ref'],
            'pedidos':len(s['pedidos']),'clientes':len(s['clientes']),
            'top_refs':s['refs'].most_common(3),
            'ok':s['total'] >= META_SEMANAL,
            'pct_meta':round(s['total']/META_SEMANAL*100),
            'export':s['export'],'pe':s['pe'],
            'mi':s['mi'],
            'pct_mi':round(s['mi']/s['total']*100,1) if s['total'] else 0,
            'convencional':s['convencional'],'montado':s['montado'],
            'pct_conv':round(s['convencional']/s['total']*100,1) if s['total'] else 0,
            'pct_mont':round(s['montado']/s['total']*100,1) if s['total'] else 0,
            'pct_export':round(s['export']/s['total']*100,1) if s['total'] else 0,
            'pct_pe':round(s['pe']/s['total']*100,1) if s['total'] else 0,
        }

    # Serializar meses
    meses_out = {}
    for k in sorted(meses):
        if k < '2026-01': continue
        m2 = meses[k]
        num_sems = len(m2['semanas_keys'])
        meta_mes = META_SEMANAL * max(num_sems, 1)
        meses_out[k] = {
            'CLASSIC':m2['CLASSIC'],'EVA':m2['EVA'],'WORKS':m2['WORKS'],
            'FIT':m2['FIT'],'OUTROS':m2['OUTROS'],
            'total':m2['total'],'label':m2['label'],
            'pedidos':len(m2['pedidos']),
            'top_refs':m2['refs'].most_common(3),
            'meta_mes':meta_mes,'num_semanas':num_sems,
            'ok':m2['total'] >= meta_mes,
            'pct_meta':round(m2['total']/meta_mes*100) if meta_mes else 0,
            'export':m2['export'],'pe':m2['pe'],
            'mi':m2['mi'],
            'pct_mi':round(m2['mi']/m2['total']*100,1) if m2['total'] else 0,
            'convencional':m2['convencional'],'montado':m2['montado'],
            'pct_conv':round(m2['convencional']/m2['total']*100,1) if m2['total'] else 0,
            'pct_mont':round(m2['montado']/m2['total']*100,1) if m2['total'] else 0,
            'pct_export':round(m2['export']/m2['total']*100,1) if m2['total'] else 0,
            'pct_pe':round(m2['pe']/m2['total']*100,1) if m2['total'] else 0,
        }

    # *** GRAVAR dados_refs_tabela.json ***
    sems_rp = {}
    for k in sorted(refs_por_semana):
        if k >= '2026-05-04':
            sems_rp[k] = {'label': sem_labels_rp.get(k,''), 'refs': dict(refs_por_semana[k].most_common())}
    meses_rp = {}
    for k in sorted(refs_por_mes):
        if k >= '2026-01':
            meses_rp[k] = {'label': mes_labels_rp.get(k,''), 'refs': dict(refs_por_mes[k].most_common())}
    with open(os.path.join(output_dir, 'dados_refs_tabela.json'), 'w', encoding='utf-8') as f_:
        json.dump({'semanas': sems_rp, 'meses': meses_rp}, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_refs_tabela.json gerado")

    # *** GRAVAR dados_programacao_detalhe.json (drilldown por semana) ***
    detalhe_out = {}
    for sem_key, itens in detalhe_raw.items():
        detalhe_out[sem_key] = [
            {'cliente': c, 'pedido': p, 'ref': r, 'plano': pl, 'dt_plano': dtp, 'pares': qtd}
            for (c, p, r, pl, dtp), qtd in itens.items()
        ]
    with open(os.path.join(output_dir, 'dados_programacao_detalhe.json'), 'w', encoding='utf-8') as f_:
        json.dump({
            'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'cutoff': cutoff_detalhe.strftime('%Y-%m-%d'),
            'semanas': detalhe_out,
        }, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_programacao_detalhe.json gerado ({sum(len(v) for v in detalhe_out.values())} itens em {len(detalhe_out)} semanas)")

    dados_prog = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'semanas':   semanas_out,
        'meses':     meses_out,
        'refs_top15': refs_prog.most_common(15),
    }
    with open(os.path.join(output_dir, 'dados_programacao.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados_prog, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_programacao.json gerado ({len(semanas_out)} semanas, {len(meses_out)} meses)")

    return {'semanas': semanas_out, 'refs_top20': refs_prog.most_common(20)}

# ─── PEDIDO EM CARTEIRA ──────────────────────────────────────────────
def processar_carteira(linhas, output_dir='.'):
    """Pedidos vendidos (canais MI/ME) que ainda NÃO foram enviados para
    produção — ou seja, sem plano de produção vinculado.

    Regras de inclusão de uma linha:
      - canal (abr_grp) é MI ou ME (E-commerce nunca gera carteira a
        produzir, fica de fora)
      - planoproducao vazio / "Não se aplica" (ainda não está na programação)
      - cod_esp_ent_sai em (1=Programado, 31=Venda Mista) — espécie 22
        (Pronta Entrega) não entra, pois já está pronta
      - LocalEstoque == '30' (local que alimenta a programação) — vale
        tanto para espécie 1 quanto 31
      - pos_item não é 'Cancelado' nem 'Faturado' (carteira aberta de
        verdade: "Nada faturado"/"Parcialmente faturado")
    """
    print("\n  Processando carteira...")

    pedidos_set = set()
    total_pares = 0
    canais = defaultdict(lambda: {'pedidos': set(), 'pares': 0})
    etapas = defaultdict(lambda: {'pedidos': set(), 'pares': 0})
    mes_entrada = defaultdict(lambda: {'label': '', 'pares': 0})
    mes_entrega = defaultdict(lambda: {'label': '', 'pares': 0})

    # Agregação por pedido (1 linha por pedido) para a tabela do dashboard
    pedidos_agg = {}

    for row in linhas:
        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        plano = g(row, IDX['plano'])
        pos_item = g(row, IDX['pos_item'])
        local = g(row, IDX['local'])

        canal = VENDA_CANAL_POR_GRUPO.get(abr)
        if canal not in ('MI', 'ME'): continue
        if cod not in ('1', '31'): continue
        plano_vazio = (not plano) or plano in ('Não se aplica', 'NÃ£o se aplica')
        if not plano_vazio: continue
        if pos_item.upper() in CARTEIRA_POS_ITEM_EXCLUIDOS: continue
        if local != '30': continue

        try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
        except: qtd = 0
        if qtd <= 0: continue

        pedido = g(row, IDX['pedido'])
        etapa = corrigir_mojibake(g(row, IDX['etapa'])) or 'NÃO INFORMADO'
        cliente = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))
        ref = g(row, IDX['ref'])
        dt_ent = parse_date(g(row, IDX['dt_ent']))
        dt_fat = parse_date(g(row, IDX['dt_fat']))

        pedidos_set.add(pedido)
        total_pares += qtd
        canais[canal]['pedidos'].add(pedido); canais[canal]['pares'] += qtd
        etapas[etapa]['pedidos'].add(pedido); etapas[etapa]['pares'] += qtd

        if dt_ent:
            k = dt_ent.strftime('%Y-%m')
            mes_entrada[k]['label'] = mes_label_curto(dt_ent)
            mes_entrada[k]['pares'] += qtd
        if dt_fat:
            k = dt_fat.strftime('%Y-%m')
            mes_entrega[k]['label'] = mes_label_curto(dt_fat)
            mes_entrega[k]['pares'] += qtd

        pa = pedidos_agg.get(pedido)
        if not pa:
            pa = pedidos_agg[pedido] = {
                'pedido': pedido, 'cliente': cliente, 'canal': canal,
                'etapa': etapa, 'pares': 0, 'refs': Counter(),
                'dt_entrada': dt_ent, 'dt_faturam': dt_fat,
            }
        pa['pares'] += qtd
        if ref: pa['refs'][ref] += qtd
        if dt_ent and (not pa['dt_entrada'] or dt_ent < pa['dt_entrada']):
            pa['dt_entrada'] = dt_ent
        if dt_fat and (not pa['dt_faturam'] or dt_fat < pa['dt_faturam']):
            pa['dt_faturam'] = dt_fat

    print(f"    Pedidos em carteira: {len(pedidos_set):,} ({total_pares:,} pares)")

    canais_out = {c: {'pedidos': len(v['pedidos']), 'pares': v['pares']}
                   for c, v in canais.items()}
    for c in ('MI', 'ME'):
        canais_out.setdefault(c, {'pedidos': 0, 'pares': 0})

    etapas_out = [
        {'etapa': e, 'pedidos': len(v['pedidos']), 'pares': v['pares']}
        for e, v in sorted(etapas.items(), key=lambda x: -x[1]['pares'])
    ]

    pedidos_out = []
    for pa in sorted(pedidos_agg.values(), key=lambda x: x['dt_faturam'] or datetime.max):
        top_ref = pa['refs'].most_common(1)
        pedidos_out.append({
            'pedido': pa['pedido'], 'cliente': pa['cliente'][:40], 'canal': pa['canal'],
            'etapa': pa['etapa'], 'pares': pa['pares'],
            'ref_principal': top_ref[0][0] if top_ref else '',
            'qtd_refs': len(pa['refs']),
            'refs': dict(pa['refs'].most_common()),
            'dt_entrada': pa['dt_entrada'].strftime('%d/%m/%Y') if pa['dt_entrada'] else '',
            'dt_faturam': pa['dt_faturam'].strftime('%d/%m/%Y') if pa['dt_faturam'] else '',
        })

    dados_cart = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'total_pedidos': len(pedidos_set),
        'total_pares': total_pares,
        'canais': canais_out,
        'etapas': etapas_out,
        'mes_entrada': dict(sorted(mes_entrada.items())),
        'mes_entrega': dict(sorted(mes_entrega.items())),
        'pedidos': pedidos_out,
    }
    with open(os.path.join(output_dir, 'dados_carteira.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados_cart, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_carteira.json gerado")

    return {
        'total_pedidos': len(pedidos_set), 'total_pares': total_pares,
        'canais': canais_out,
    }

# ─── ESTOQUE ─────────────────────────────────────────────────────────
def processar_estoque(arquivo_esqt, output_dir='.'):
    print("\n  Processando estoque...")
    ext = os.path.splitext(arquivo_esqt)[1].lower()

    def parse_n(s):
        try: return float(str(s).replace(',', '.').strip())
        except: return 0.0

    # Lê o arquivo (CSV ou XLS) e normaliza em lista de tuplas
    rows_norm = []
    if ext == '.csv':
        sep = detectar_sep(arquivo_esqt)
        with open(arquivo_esqt, 'r', encoding='utf-8', errors='replace') as f_:
            reader = csv.reader(f_, delimiter=sep)
            next(reader)  # pula cabeçalho
            for row in reader:
                def _g(c, _r=row):
                    try: return str(_r[c]).strip()
                    except: return ''
                local = _g(1)
                if local.endswith('.0'): local = local[:-2]
                locest = _g(13)
                if locest.endswith('.0'): locest = locest[:-2]
                rows_norm.append((local, _g(2), _g(3), _g(5),
                                  parse_n(_g(8)),  parse_n(_g(9)),
                                  parse_n(_g(10)), parse_n(_g(11)), parse_n(_g(12)),
                                  locest, _g(15)))
    else:
        try: import xlrd
        except ImportError:
            print("    ✗ xlrd não instalado. Rode: pip install xlrd"); sys.exit(1)
        wb = xlrd.open_workbook(arquivo_esqt)
        ws = wb.sheet_by_index(0)
        for r in range(1, ws.nrows):
            def ge(c, _r=r): return str(ws.cell_value(_r, c)).strip()
            local = ge(1)
            if local.endswith('.0'): local = local[:-2]
            locest = ge(13) if ws.ncols > 13 else ''
            if locest.endswith('.0'): locest = locest[:-2]
            gradecode = ge(15) if ws.ncols > 15 else ''
            rows_norm.append((local, ge(2), ge(3), ge(5),
                              parse_n(ge(8)),  parse_n(ge(9)),
                              parse_n(ge(10)), parse_n(ge(11)), parse_n(ge(12)),
                              locest, gradecode))

    # Grade fechada → numeração: indexada por código (1:1 com LocEst no mapa).
    GRADE_BY_CODE = {v['code']: {'locest': k, **v} for k, v in GRADES_NUMERACAO.items()}
    ABERTA = '__ABERTA__'  # chave interna para estoque sem grade fechada

    def grade_breakdown(code, livre):
        """Quebra os pares livres de uma grade fechada mapeada em
        grades completas + avulsos + pares por numeração (base: livre)."""
        info = GRADE_BY_CODE.get(code)
        if not info:
            return None
        tot = sum(info['pattern']) or 1
        livre_i = int(round(livre))
        completas = livre_i // tot
        avulso = livre_i - completas * tot
        return {'code': code, 'tot': tot, 'completas': completas, 'avulso': avulso,
                'sizes': info['sizes'], 'pattern': info['pattern'],
                'per_size': {s: completas * p for s, p in zip(info['sizes'], info['pattern'])}}

    estoque = defaultdict(lambda: {
        'fisico':0,'reservas':0,'livre':0,'vlr_fisico':0.0,'vlr_livre':0.0,'locais':set(),
        'grades': defaultdict(lambda: {'livre':0,'fisico':0}),   # ref -> code/ABERTA -> {}
        'linhas': defaultdict(lambda: {
            'fisico':0,'reservas':0,'livre':0,'locais':set(),
            'combinacoes': defaultdict(lambda: {
                'fisico':0,'reservas':0,'livre':0,
                'grades': defaultdict(lambda: {'livre':0,'fisico':0})
            })
        })
    })
    grade_glob = defaultdict(lambda: {'livre':0,'fisico':0})   # code/ABERTA -> {} (global)

    for local, ref, descr, comb, fisico, reservas, livre, vlr_f, vlr_l, locest, gradecode in rows_norm:
        if local not in LOCAIS_ESTOQUE: continue
        # Código da grade: usa o mapa por LocEst (mais confiável que o texto).
        gmap = GRADES_NUMERACAO.get(locest)
        gkey = (gmap['code'] if gmap else gradecode.strip()) or ABERTA
        e = estoque[ref]
        e['fisico'] += fisico; e['reservas'] += reservas; e['livre'] += livre
        e['vlr_fisico'] += vlr_f; e['vlr_livre'] += vlr_l; e['locais'].add(local)
        e['grades'][gkey]['livre'] += livre; e['grades'][gkey]['fisico'] += fisico
        grade_glob[gkey]['livre'] += livre; grade_glob[gkey]['fisico'] += fisico
        if descr:
            ln = e['linhas'][descr]
            ln['fisico'] += fisico; ln['reservas'] += reservas; ln['livre'] += livre
            ln['locais'].add(local)
            if comb:
                cv = ln['combinacoes'][comb]
                cv['fisico'] += fisico; cv['reservas'] += reservas; cv['livre'] += livre
                cv['grades'][gkey]['livre'] += livre; cv['grades'][gkey]['fisico'] += fisico
                if local == '199':
                    cv['ga'] = True  # Grade Aberta — local 199

    total_f = sum(d['fisico'] for d in estoque.values())
    total_r = sum(d['reservas'] for d in estoque.values())
    total_l = sum(d['livre'] for d in estoque.values())
    print(f"    Físico: {total_f:,.0f} | Reservas: {total_r:,.0f} | Livre: {total_l:,.0f}")

    def grades_list(grades_dict):
        """Serializa o dict de grades de uma combinação/ref para o JSON."""
        out = []
        for gkey, gv in sorted(grades_dict.items(), key=lambda x: -x[1]['livre']):
            livre = int(gv['livre']); fis = int(gv['fisico'])
            if gkey == ABERTA:
                out.append({'tipo':'aberta','livre':livre,'fisico':fis})
            else:
                gb = grade_breakdown(gkey, gv['livre'])
                if gb:
                    out.append({'tipo':'fechada','code':gkey,'livre':livre,'fisico':fis,
                                'completas':gb['completas'],'avulso':gb['avulso'],
                                'tot':gb['tot'],'sizes':gb['sizes'],'pattern':gb['pattern'],
                                'per_size':gb['per_size']})
                else:
                    out.append({'tipo':'sem_numeracao','code':gkey,'livre':livre,'fisico':fis})
        return out

    estoque_out = {}
    for ref, d in sorted(estoque.items(), key=lambda x: -x[1]['livre']):
        linhas_sorted = sorted(d['linhas'].items(), key=lambda x: -x[1]['livre'])
        descr_principal = linhas_sorted[0][0] if linhas_sorted else ''
        all_locais = sorted(set().union(*[ln['locais'] for ln in d['linhas'].values()]) if d['linhas'] else set())
        total_combs = sum(len(ln['combinacoes']) for ln in d['linhas'].values())
        estoque_out[ref] = {
            'descricaoCompleta': descr_principal,
            'fisico':int(d['fisico']),'reservas':int(d['reservas']),'livre':int(d['livre']),
            'vlr_fisico':round(d['vlr_fisico'],2),'vlr_livre':round(d['vlr_livre'],2),
            'locais': all_locais, 'qtd_combinacoes': total_combs,
            'grades': grades_list(d['grades']),
            'linhas': {
                descr: {
                    'fisico':int(ln['fisico']),'reservas':int(ln['reservas']),'livre':int(ln['livre']),
                    'locais':sorted(list(ln['locais'])),'qtd_combinacoes':len(ln['combinacoes']),
                    'combinacoes':{
                        k: {'fisico':int(v['fisico']),'reservas':int(v['reservas']),'livre':int(v['livre']),
                            'grades': grades_list(v['grades']),
                            **({'ga':True} if v.get('ga') else {})}
                        for k,v in sorted(ln['combinacoes'].items(), key=lambda x:-x[1]['livre'])}
                }
                for descr, ln in linhas_sorted
            }
        }
    totais_est = {
        'fisico':int(total_f),'reservas':int(total_r),'livre':int(total_l),
        'vlr_fisico':round(sum(d['vlr_fisico'] for d in estoque.values()),2),
        'vlr_livre':round(sum(d['vlr_livre'] for d in estoque.values()),2)
    }
    # Resumo global de grades completas disponíveis (base: livre)
    grades_global = grades_list(grade_glob)
    # Gravar dados_estoque.json
    with open(os.path.join(output_dir, 'dados_estoque.json'), 'w', encoding='utf-8') as f_:
        json.dump({
            'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'totais': totais_est,
            'grades_global': grades_global,
            'refs': estoque_out,
        }, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_estoque.json gerado ({len(estoque_out)} refs)")
    return {'refs': estoque_out, 'totais': totais_est, 'grades_global': grades_global}

# ─── FATURAMENTO ─────────────────────────────────────────────────────
ANOMESFATURA_COL = 19  # coluna anomesfatura no CSV 3YS (yyyymm da emissão NF)

# CFOPs excluídos do faturamento geral — continuam visíveis nas abas de
# CFOP e Conta Contábil (badge "não soma"), mas NÃO entram no resumo por
# grupo, nos totais, nem nas pendências retroativas. Motivo comum: não são
# venda de produto (bonificação/doação, conserto/reparo, amostras, outras
# saídas), então não compõem o faturamento nem batem com a conta contábil
# de venda.
#   5910         — remessa de bonificação / doação
#   5916 / 6916 — conserto/reparo (saída para terceiros, mesmo e intere.)
#   7949         — amostras grátis de exportação (ME)
#   5949 / 6949 — outras saídas de mercadoria não especificadas
#                  (bonificação/brinde/reposição — sem conta contábil de venda)
CFOPS_FORA_DO_GERAL = {'5910', '5916', '6916', '7949', '5949', '6949'}

# Espécies (cod_esp_ent_sai) que também não compõem o faturamento geral —
# mesmo tratamento dos CFOPS_FORA_DO_GERAL (fora do resumo por grupo, totais
# e retroativo; visíveis nas abas CFOP/Conta com "não soma").
#   132 — remessa de troca de e-commerce (não é venda; hoje sempre CFOP
#         5949/6949, mas a regra por espécie garante a exclusão mesmo que
#         apareça com outro CFOP no futuro).
ESPECIES_FORA_DO_GERAL = {'132'}

def classifica_faturamento(cod, abr, marca=''):
    """Retorna (canal, tipo) para a área de Faturamento.

    EVA      → marca=='COMPOSTOS EVA' (checado primeiro, sem filtro de
               cliente/espécie — qualquer cod_esp/abr_grp conta)
    MI PROG  → cod=1   + (MERCADO INTERNO ou ISENTO em abr_grp)
    MI PE    → cod=22  + (MERCADO INTERNO ou ISENTO em abr_grp)
    MI MISTA → cod=31  + (MERCADO INTERNO ou ISENTO em abr_grp)
    ME       → 'EXPORTA' em abr_grp, sem 'MOULD' (só EXPORTACAO - CALCADOS)
    EC       → cod=32 OU 'ECOMMERCE'/'E-COMMERCE' em abr_grp

    NÃO usa mais o gatilho cod=17 para ME: a espécie 17 aparece também em
    outros segmentos do Grupo Mould (matrizes, solas, matrizaria) que não são
    exportação de calçados Boaonda e inflavam o ME. ME agora é só o grupo de
    cadastro de exportação de calçados (abr_grp com 'EXPORTA'). Linhas de
    matriz/sola caem em None e ficam fora de todo o faturamento (grupo, CFOP,
    conta contábil), conforme decisão do usuário (2026-07-13).
    """
    if is_composto_eva(marca):
        return 'EVA', 'EVA'

    is_ec_abr  = 'ECOMMERCE' in abr or 'E-COMMERCE' in abr
    is_exporta = 'EXPORTA' in abr and 'MOULD' not in abr
    is_mi_abr  = ('MERCADO INTERNO' in abr or 'ISENTO' in abr) and 'EXPORTA' not in abr

    if cod == '32' or is_ec_abr:        return 'EC', 'EC'
    if is_exporta:                      return 'ME', 'ME'
    if is_mi_abr:
        if cod == '1':  return 'MI', 'PROG'
        if cod == '22': return 'MI', 'PE'
        if cod == '31': return 'MI', 'MISTA'
    return None, None

RETRO_CANAL_LABELS = {
    'MI_PROG':  ('MI Programado',    'BRL', 'pares'),
    'MI_PE':    ('MI Pronta Entrega','BRL', 'pares'),
    'MI_MISTA': ('MI Venda Mista',   'BRL', 'pares'),
    'ME':       ('Mercado Externo',  'USD', 'pares'),
    'EC':       ('E-Commerce',       'BRL', 'pares'),
    'EVA':      ('Composto EVA',     'BRL', 'kg'),
}

def processar_faturamento(linhas, output_dir='.', taxa_cambio_me=5.0):
    """Gera dados_faturamento.json — faturamento realizado e previsto por
    canal/espécie e mês de referência, com suporte a taxa de câmbio ME."""
    print("\n  Processando faturamento...")
    mes_atual_fat = datetime.now().strftime('%Y%m')

    # Acumuladores: [fat_vlr, prev_vlr, fat_pares, prev_pares]
    # ME usa USD em índice 0/1; MI/EC usam BRL
    def new_mes():
        return {
            'MI': {'PROG':[0.0,0.0,0,0],'PE':[0.0,0.0,0,0],'MISTA':[0.0,0.0,0,0]},
            'ME': [0.0,0.0,0,0],
            'EC': [0.0,0.0,0,0],
            'EVA': [0.0,0.0,0.0,0.0],  # kg (float, não pares) nos índices 2/3
        }
    dados = defaultdict(new_mes)
    # dados_cfop[mes_ref][cfop] = [fat_brl, prev_brl, fat_usd, prev_usd,
    #                              fat_pares, prev_pares, fat_kg, prev_kg]
    # pares (índices 4/5) = MI/ME/EC; kg (índices 6/7) = Composto EVA —
    # unidades distintas, nunca somadas entre si.
    dados_cfop = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0]))
    # dados_conta[mes_ref][conta_contabil][cfop] = mesma estrutura de índices que dados_cfop
    # O nível intermediário (conta) acumula os valores por CFOP para permitir
    # o drilldown "conta → CFOPs que a compõem" no frontend.
    dados_conta = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0])))
    # dados_clientes[mes_ref][canal][cliente] = [fat_vlr, prev_vlr, fat_qtd, prev_qtd]
    # canal ME (USD/pares) e EVA (BRL/kg) — drilldown por cliente no resumo
    # mensal por grupo. MI/EC não têm volume de clientes que justifique o
    # drilldown (muito mais pulverizado) e não foram solicitados.
    dados_clientes = defaultdict(lambda: {'ME': defaultdict(lambda: [0.0, 0.0, 0, 0]),
                                           'EVA': defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])})
    # dados_pedidos_eva[mes_ref][cliente][pedido] = [fat_vlr, prev_vlr, fat_kg, prev_kg]
    # 2º nível do drilldown de Composto EVA — só EVA, conforme solicitado.
    dados_pedidos_eva = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])))
    # dados_pedidos_mi[tipo][mes_ref][pedido] = {cliente, etapa, refs:{ref:[rv,pv,rq,pq]}, rv, pv, rq, pq}
    # Drilldown de MI Venda Mista (espécie 31) e MI Pronta Entrega (espécie
    # 22) — lista plana de pedidos, para auditoria visual do número
    # (investigar possível discrepância apontada pelo usuário frente a outra
    # fonte de referência). O detalhe por referência só é exibido em um 2º
    # nível, ao expandir o pedido; a etapa só é exibida em um agrupamento
    # alternativo, e só para pedidos ainda em aberto (previsto).
    def _novo_pedido_mi():
        return {'cliente': '', 'etapa': '', 'refs': defaultdict(lambda: [0.0, 0.0, 0, 0]),
                'rv': 0.0, 'pv': 0.0, 'rq': 0, 'pq': 0,
                'planos': set(), 'dt_plano_max': None}
    dados_pedidos_mi = {'MISTA': defaultdict(lambda: defaultdict(_novo_pedido_mi)),
                         'PE': defaultdict(lambda: defaultdict(_novo_pedido_mi))}
    # dados_retro[mes_ref][canal_tipo][pedido] — previsto de meses anteriores ao atual,
    # para o painel de pendências retroativas (todos os canais, detalhe por pedido e ref).
    def _novo_pedido_retro():
        return {'cliente': '', 'etapa': '', 'refs': defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]),
                'pv': 0.0, 'pq': 0.0, 'planos': set(), 'dt_plano_max': None}
    dados_retro = defaultdict(lambda: defaultdict(lambda: defaultdict(_novo_pedido_retro)))
    sem_data_list = []   # [{ref, canal, especie, pares, valor}]
    # Detalhe linha-a-linha por mês e canal, para exportação de conferência
    # (botão "Exportar detalhe" no painel). detalhe[mes_ref][canal] = [linhas].
    detalhe = defaultdict(lambda: defaultdict(list))
    total_fat = total_prev = sem_data_count = 0

    for row in linhas:
        pos = g(row, IDX['pos_item']).strip().upper()
        if pos == 'CANCELADO': continue
        if pos == 'FATURADO':                                   status = 'fat'
        elif pos in ('NADA FATURADO','PARCIALMENTE FATURADO'):  status = 'prev'
        else: continue

        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        if cod in ESPECIES_ORCAMENTO_ME: continue   # orçamento ME — jamais faturado
        marca = g(row, IDX['marca'])
        canal, tipo = classifica_faturamento(cod, abr, marca)
        if not canal: continue

        try: qtd_raw = float(g(row, IDX['qtd']).replace(',','.'))
        except: qtd_raw = 0.0
        if qtd_raw <= 0: continue
        qtd = qtd_raw if canal == 'EVA' else int(qtd_raw)  # EVA é kg (float), demais são pares

        # Faturamento usa o VALOR TOTAL (bruto: com IPI/frete), para bater com
        # os valores contábeis (faturamento bruto). Se a coluna de valor total
        # vier vazia/ausente (ex.: view MySQL sem 'valortotal'), cai para o
        # valor líquido para não zerar o faturamento.
        try: vlr = float(g(row, IDX['vlr_total']).replace(',','.'))
        except: vlr = 0.0
        if vlr == 0.0:
            try: vlr = float(g(row, IDX['vlr']).replace(',','.'))
            except: vlr = 0.0

        # Mês de referência
        if status == 'fat':
            anomes_fat = g(row, ANOMESFATURA_COL)
            if anomes_fat and len(anomes_fat) == 6 and anomes_fat.isdigit():
                mes_ref = anomes_fat
            else:
                dt_f = parse_date(g(row, IDX['dt_fat']))
                mes_ref = dt_f.strftime('%Y%m') if dt_f else None
        else:
            dt_f = parse_date(g(row, IDX['dt_fat']))
            dt_p = parse_date(g(row, IDX['dt_plano']))
            if dt_f and dt_p:   mes_ref = max(dt_f, dt_p).strftime('%Y%m')
            elif dt_f:          mes_ref = dt_f.strftime('%Y%m')
            elif dt_p:          mes_ref = dt_p.strftime('%Y%m')
            else:               mes_ref = None

        if mes_ref is None:
            sem_data_list.append({'ref': g(row, IDX['ref']), 'canal': canal, 'especie': tipo,
                                  'pares': qtd, 'valor': round(vlr, 2)})
            sem_data_count += 1
            continue

        cfop_code = g(row, IDX['cfop'])
        # "Fora do geral": não compõe resumo por grupo/totais/retroativo (mas
        # fica visível em CFOP/Conta com "não soma"). Vale por CFOP
        # (conserto/reparo, bonificação, outras saídas) OU por espécie
        # (132 = remessa de troca de e-commerce).
        eh_fora_geral = (cfop_code in CFOPS_FORA_DO_GERAL
                         or cod in ESPECIES_FORA_DO_GERAL)

        # Detalhe para exportação de conferência (todas as linhas que compõem
        # o faturamento do canal/mês, inclusive as "não soma", sinalizadas).
        try: vlr_liq = round(float(g(row, IDX['vlr']).replace(',', '.')), 2)
        except: vlr_liq = 0.0
        detalhe[mes_ref][canal].append({
            'pedido': g(row, IDX['pedido']),
            'cliente': corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:60],
            'ref': g(row, IDX['ref']),
            'descr': corrigir_mojibake(g(row, IDX['descr'])),
            'especie': cod,
            'tipo': tipo,
            'cfop': cfop_code,
            'conta': g(row, IDX['conta_contabil']).strip(),
            'grupo': g(row, IDX['abr_grp']),
            'qtd': qtd,
            'vlr_liq': vlr_liq,
            'vlr_bruto': round(vlr, 2),
            'dt_ent': g(row, IDX['dt_ent']),
            'dt_fat': g(row, IDX['dt_fat']),
            'dt_plano': g(row, IDX['dt_plano']),
            'mes_ref': mes_ref,
            'status': 'Realizado' if status == 'fat' else 'Previsto',
            'nao_soma': 'Sim' if eh_fora_geral else 'Não',
        })

        vi, qi = (0, 2) if status == 'fat' else (1, 3)
        if not eh_fora_geral:
            m = dados[mes_ref]
            if canal == 'MI':
                m['MI'][tipo][vi] += vlr; m['MI'][tipo][qi] += qtd
            elif canal == 'ME':
                m['ME'][vi] += vlr; m['ME'][qi] += qtd     # USD
            elif canal == 'EVA':
                m['EVA'][vi] += vlr; m['EVA'][qi] += qtd   # kg
            else:
                m['EC'][vi] += vlr; m['EC'][qi] += qtd

            if canal in ('ME', 'EVA'):
                cliente = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:40] or '(sem nome)'
                dc_cli = dados_clientes[mes_ref][canal][cliente]
                dc_cli[vi] += vlr; dc_cli[qi] += qtd
                if canal == 'EVA':
                    pedido = g(row, IDX['pedido']).strip() or '(sem pedido)'
                    dp_ped = dados_pedidos_eva[mes_ref][cliente][pedido]
                    dp_ped[vi] += vlr; dp_ped[qi] += qtd

            if canal == 'MI' and tipo in ('MISTA', 'PE'):
                pedido = g(row, IDX['pedido']).strip() or '(sem pedido)'
                cliente = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:40] or '(sem nome)'
                ref = g(row, IDX['ref']).strip() or '(sem referência)'
                pm = dados_pedidos_mi[tipo][mes_ref][pedido]
                pm['cliente'] = cliente
                # Etapa só é relevante para pedidos ainda em aberto (previsto) —
                # uma vez faturado, o campo no ERP fica obsoleto/sem sentido.
                if status == 'prev':
                    pm['etapa'] = corrigir_mojibake(g(row, IDX['etapa'])) or 'NÃO INFORMADO'
                # Plano de produção — só quando a linha já está alocada a um
                # plano (campo preenchido e diferente de "Não se aplica").
                plano = g(row, IDX['plano']).strip()
                if plano and plano not in ('Não se aplica', 'NÃ£o se aplica'):
                    pm['planos'].add(plano)
                    dt_p = parse_date(g(row, IDX['dt_plano']))
                    if dt_p and (not pm['dt_plano_max'] or dt_p > pm['dt_plano_max']):
                        pm['dt_plano_max'] = dt_p
                rf = pm['refs'][ref]
                rf[vi] += vlr; rf[qi] += qtd
                if status == 'fat': pm['rv'] += vlr; pm['rq'] += qtd
                else:                pm['pv'] += vlr; pm['pq'] += qtd

            if status == 'fat':  total_fat  += qtd
            else:                total_prev += qtd

        # Acumular por CFOP — valor (BRL/USD) soma todos os canais; a
        # quantidade vai para pares (MI/ME/EC) ou kg (EVA), nunca somadas.
        # Conserto/reparo entra aqui mesmo estando fora do resumo por
        # grupo, para permanecer visível na aba de CFOP.
        vi_c = (2 if canal == 'ME' else 0) + (0 if status == 'fat' else 1)
        qi_c = (6 if status == 'fat' else 7) if canal == 'EVA' else (4 if status == 'fat' else 5)
        if cfop_code:
            dc = dados_cfop[mes_ref][cfop_code]
            dc[vi_c] += vlr
            dc[qi_c] += qtd
        # Retroativo: previsto de meses anteriores — painel de acompanhamento de pendências.
        # Exclui itens fora do geral (conserto/reparo, bonificação, outras
        # saídas, remessa de troca) — não são faturamentos de produto.
        if status == 'prev' and mes_ref < mes_atual_fat and not eh_fora_geral:
            chave_retro = f'MI_{tipo}' if canal == 'MI' else canal
            pedido_r  = g(row, IDX['pedido']).strip() or '(sem pedido)'
            cliente_r = corrigir_mojibake(g(row, IDX['nomeholder']) or g(row, IDX['razao']))[:40] or '(sem nome)'
            ref_r     = g(row, IDX['ref']).strip() or '(sem referência)'
            dr = dados_retro[mes_ref][chave_retro][pedido_r]
            dr['cliente'] = cliente_r
            dr['etapa']   = corrigir_mojibake(g(row, IDX['etapa'])) or 'NÃO INFORMADO'
            plano_r = g(row, IDX['plano']).strip()
            if plano_r and plano_r not in ('Não se aplica', 'NÃ£o se aplica'):
                dr['planos'].add(plano_r)
                dt_p_r = parse_date(g(row, IDX['dt_plano']))
                if dt_p_r and (not dr['dt_plano_max'] or dt_p_r > dr['dt_plano_max']):
                    dr['dt_plano_max'] = dt_p_r
            dr['refs'][ref_r][1] += vlr
            dr['refs'][ref_r][3] += qtd
            dr['pv'] += vlr
            dr['pq'] += qtd

        # Acumular por conta contábil (aninhado por CFOP para drilldown).
        # Linhas sem conta preenchida entram no balde especial para que o
        # total da aba Conta Contábil bata com o total da aba Grupo.
        conta_code = g(row, IDX['conta_contabil']).strip() or '(sem conta contábil)'
        cfop_sub = cfop_code or '(sem CFOP)'
        dc2 = dados_conta[mes_ref][conta_code][cfop_sub]
        dc2[vi_c] += vlr
        dc2[qi_c] += qtd

    print(f"    Faturado: {total_fat:,} pares | Previsto: {total_prev:,} pares | Sem data: {sem_data_count}")

    def build_mes(m):
        mi = {}
        for t in ('PROG','PE','MISTA'):
            rv, pv, rq, pq = m['MI'][t]
            if rv or pv or rq or pq:
                mi[t] = {'REALIZADO': round(rv, 2), 'PREVISTO': round(pv, 2),
                         'REALIZADO_PARES': int(rq), 'PREVISTO_PARES': int(pq)}
        return {
            'MI': mi,
            'ME': {'REALIZADO_USD': round(m['ME'][0], 2), 'PREVISTO_USD': round(m['ME'][1], 2),
                   'REALIZADO_PARES': int(m['ME'][2]), 'PREVISTO_PARES': int(m['ME'][3])},
            'EC': {'REALIZADO': round(m['EC'][0], 2), 'PREVISTO': round(m['EC'][1], 2),
                   'REALIZADO_PARES': int(m['EC'][2]), 'PREVISTO_PARES': int(m['EC'][3])},
            'EVA': {'REALIZADO': round(m['EVA'][0], 2), 'PREVISTO': round(m['EVA'][1], 2),
                    'REALIZADO_KG': round(m['EVA'][2], 1), 'PREVISTO_KG': round(m['EVA'][3], 1)},
        }

    dados_out    = {k: build_mes(m) for k, m in sorted(dados.items())}
    sem_data_out = sorted(sem_data_list, key=lambda x: (-x['pares'], x['ref']))

    def build_cfop_mes(cfops):
        out = {}
        for cfop_code, acc in sorted(cfops.items()):
            fb, pb, fu, pu, fq, pq, fkg, pkg = acc
            if any([fb, pb, fu, pu, fq, pq, fkg, pkg]):
                entry = {
                    'REALIZADO': round(fb, 2), 'PREVISTO': round(pb, 2),
                    'REALIZADO_USD': round(fu, 2), 'PREVISTO_USD': round(pu, 2),
                    'REALIZADO_PARES': int(fq), 'PREVISTO_PARES': int(pq),
                }
                if fkg or pkg:
                    entry['REALIZADO_KG'] = round(fkg, 1)
                    entry['PREVISTO_KG'] = round(pkg, 1)
                if cfop_code in CFOPS_FORA_DO_GERAL:
                    entry['FORA_DO_GERAL'] = True
                out[cfop_code] = entry
        return out

    dados_cfop_out = {k: build_cfop_mes(v) for k, v in sorted(dados_cfop.items())}

    def build_conta_mes(contas_map):
        out = {}
        for conta_code, cfop_map in sorted(contas_map.items()):
            cfops_out = {}
            # total exclui conserto/reparo (mesma lógica do resumo por grupo)
            total = [0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0]
            for cfop_sub, acc in sorted(cfop_map.items()):
                fb, pb, fu, pu, fq, pq, fkg, pkg = acc
                if not any([fb, pb, fu, pu, fq, pq, fkg, pkg]):
                    continue
                entry = {'REALIZADO': round(fb, 2), 'PREVISTO': round(pb, 2),
                         'REALIZADO_USD': round(fu, 2), 'PREVISTO_USD': round(pu, 2),
                         'REALIZADO_PARES': int(fq), 'PREVISTO_PARES': int(pq)}
                if fkg or pkg:
                    entry['REALIZADO_KG'] = round(fkg, 1)
                    entry['PREVISTO_KG'] = round(pkg, 1)
                if cfop_sub in CFOPS_FORA_DO_GERAL:
                    entry['FORA_DO_GERAL'] = True
                else:
                    for i, v in enumerate(acc):
                        total[i] += v
                cfops_out[cfop_sub] = entry
            if not cfops_out:
                continue
            fb, pb, fu, pu, fq, pq, fkg, pkg = total
            conta_entry = {'REALIZADO': round(fb, 2), 'PREVISTO': round(pb, 2),
                           'REALIZADO_USD': round(fu, 2), 'PREVISTO_USD': round(pu, 2),
                           'REALIZADO_PARES': int(fq), 'PREVISTO_PARES': int(pq),
                           'cfops': cfops_out}
            if fkg or pkg:
                conta_entry['REALIZADO_KG'] = round(fkg, 1)
                conta_entry['PREVISTO_KG'] = round(pkg, 1)
            if conta_code == '(sem conta contábil)':
                conta_entry['SEM_CONTA'] = True
            out[conta_code] = conta_entry
        return out

    dados_conta_out = {k: build_conta_mes(v) for k, v in sorted(dados_conta.items())}

    def build_retro_mes(grupos_map):
        out = {}
        for chave, pedidos_map in sorted(grupos_map.items()):
            label, moeda, unidade = RETRO_CANAL_LABELS.get(chave, (chave, 'BRL', 'pares'))
            pedidos = []
            total_pv = 0.0; total_pq = 0.0
            for pedido, d in pedidos_map.items():
                if not d['pv'] and not d['pq']:
                    continue
                refs = []
                for ref, acc in sorted(d['refs'].items()):
                    pv_r, pq_r = acc[1], acc[3]
                    if not pv_r and not pq_r:
                        continue
                    refs.append({'ref': ref, 'PREVISTO': round(pv_r, 2),
                                 'PREVISTO_PARES': round(pq_r, 1) if unidade == 'kg' else int(pq_r)})
                refs.sort(key=lambda r: -r['PREVISTO'])
                planos_sorted = sorted(d['planos'])
                pedidos.append({
                    'pedido': pedido, 'cliente': d['cliente'], 'etapa': d['etapa'],
                    'plano': planos_sorted[0] if planos_sorted else '',
                    'qtd_planos': len(planos_sorted),
                    'dt_plano': d['dt_plano_max'].strftime('%d/%m/%Y') if d['dt_plano_max'] else '',
                    'PREVISTO': round(d['pv'], 2),
                    'PREVISTO_PARES': round(d['pq'], 1) if unidade == 'kg' else int(d['pq']),
                    'refs': refs,
                })
                total_pv += d['pv']; total_pq += d['pq']
            if not pedidos:
                continue
            pedidos.sort(key=lambda p: -p['PREVISTO'])
            out[chave] = {'label': label, 'moeda': moeda, 'unidade': unidade,
                          'PREVISTO': round(total_pv, 2),
                          'PREVISTO_PARES': round(total_pq, 1) if unidade == 'kg' else int(total_pq),
                          'pedidos': pedidos}
        return out

    dados_retro_out = {}
    for _kr, _vr in sorted(dados_retro.items(), reverse=True):
        _built = build_retro_mes(_vr)
        if _built:
            dados_retro_out[_kr] = _built

    def build_clientes_mes(mc, mes_ref):
        out = {}
        for canal in ('ME', 'EVA'):
            entries = []
            for cliente, (rv, pv, rq, pq) in mc[canal].items():
                if not any([rv, pv, rq, pq]):
                    continue
                if canal == 'ME':
                    entries.append({'cliente': cliente, 'REALIZADO_USD': round(rv, 2),
                                     'PREVISTO_USD': round(pv, 2),
                                     'REALIZADO_PARES': int(rq), 'PREVISTO_PARES': int(pq)})
                else:
                    entry = {'cliente': cliente, 'REALIZADO': round(rv, 2),
                              'PREVISTO': round(pv, 2),
                              'REALIZADO_KG': round(rq, 1), 'PREVISTO_KG': round(pq, 1)}
                    pedidos = []
                    for pedido, (prv, ppv, prq, ppq) in dados_pedidos_eva.get(mes_ref, {}).get(cliente, {}).items():
                        if not any([prv, ppv, prq, ppq]):
                            continue
                        pedidos.append({'pedido': pedido, 'REALIZADO': round(prv, 2),
                                         'PREVISTO': round(ppv, 2),
                                         'REALIZADO_KG': round(prq, 1), 'PREVISTO_KG': round(ppq, 1)})
                    pedidos.sort(key=lambda p: -(p['REALIZADO'] + p['PREVISTO']))
                    entry['pedidos'] = pedidos
                    entries.append(entry)
            entries.sort(key=lambda e: -(e.get('REALIZADO_USD', e.get('REALIZADO', 0)) +
                                          e.get('PREVISTO_USD', e.get('PREVISTO', 0))))
            if entries:
                out[canal] = entries
        return out

    dados_clientes_out = {k: build_clientes_mes(v, k) for k, v in sorted(dados_clientes.items())}

    def build_pedidos_mi_mes(pm):
        pedidos = []
        for pedido, d in pm.items():
            if not any([d['rv'], d['pv'], d['rq'], d['pq']]):
                continue
            refs = []
            for ref, (rv, pv, rq, pq) in d['refs'].items():
                if not any([rv, pv, rq, pq]):
                    continue
                refs.append({'ref': ref, 'REALIZADO': round(rv, 2), 'PREVISTO': round(pv, 2),
                             'REALIZADO_PARES': int(rq), 'PREVISTO_PARES': int(pq)})
            refs.sort(key=lambda r: -(r['REALIZADO'] + r['PREVISTO']))
            planos_sorted = sorted(d['planos'])
            pedidos.append({'pedido': pedido, 'cliente': d['cliente'], 'etapa': d['etapa'],
                             'plano': planos_sorted[0] if planos_sorted else '',
                             'qtd_planos': len(planos_sorted),
                             'dt_plano': d['dt_plano_max'].strftime('%d/%m/%Y') if d['dt_plano_max'] else '',
                             'REALIZADO': round(d['rv'], 2), 'PREVISTO': round(d['pv'], 2),
                             'REALIZADO_PARES': int(d['rq']), 'PREVISTO_PARES': int(d['pq']),
                             'refs': refs})
        pedidos.sort(key=lambda p: -(p['REALIZADO'] + p['PREVISTO']))
        return pedidos

    dados_pedidos_mista_out = {k: build_pedidos_mi_mes(v) for k, v in sorted(dados_pedidos_mi['MISTA'].items())}
    dados_pedidos_pe_out = {k: build_pedidos_mi_mes(v) for k, v in sorted(dados_pedidos_mi['PE'].items())}

    result = {'gerado_em':datetime.now().strftime('%d/%m/%Y %H:%M'),
              'taxa_cambio_me':taxa_cambio_me, 'dados':dados_out,
              'dados_cfop':dados_cfop_out, 'dados_conta':dados_conta_out,
              'dados_clientes':dados_clientes_out,
              'dados_pedidos_mista':dados_pedidos_mista_out,
              'dados_pedidos_pe':dados_pedidos_pe_out,
              'dados_retroativos':dados_retro_out,
              'sem_data':sem_data_out}
    with open(os.path.join(output_dir,'dados_faturamento.json'),'w',encoding='utf-8') as f_:
        json.dump(result, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_faturamento.json gerado ({len(dados_out)} meses)")

    # Detalhe de conferência — 1 arquivo por mês (dados_faturamento_det_AAAAMM
    # .json = {canal: [linhas]}). Grandes (~MB/mês) → NÃO commitados; ficam no
    # volume e são lidos sob demanda pelo endpoint /api/faturamento/detalhe.
    for antigo in glob.glob(os.path.join(output_dir, 'dados_faturamento_det_*.json')):
        try: os.remove(antigo)
        except OSError: pass
    for mes, canais in detalhe.items():
        with open(os.path.join(output_dir, f'dados_faturamento_det_{mes}.json'),
                  'w', encoding='utf-8') as f_:
            json.dump({c: linhas for c, linhas in canais.items()}, f_,
                      ensure_ascii=False, separators=(',', ':'))
    print(f"    ✓ detalhe de faturamento gerado ({len(detalhe)} meses)")
    return result

# ─── JSON PORTAL ─────────────────────────────────────────────────────
def gerar_json_portal(vendas, prog, estoque, carteira, mes_atual, mes_label, output_dir='.', fat=None):
    agora = datetime.now()
    ano = int(mes_atual[:4]); mes = int(mes_atual[4:])
    mes_ref_atual = f"{ano}-{mes:02d}"
    # Uma semana pertence ao mês cujo último dia útil (sexta-feira) ela contém
    # — ver 'mes_ref' calculado em processar_programacao.
    sems_mes = {k:v for k,v in prog['semanas'].items()
                if v.get('mes_ref')==mes_ref_atual}

    # Faturamento — resumo do mês atual para o card da home (estrutura nova)
    fat_dados = (fat or {}).get('dados', {})
    mes_fat_d = fat_dados.get(mes_atual, {})
    mi_d  = mes_fat_d.get('MI', {})
    me_d  = mes_fat_d.get('ME', {})
    ec_d  = mes_fat_d.get('EC', {})
    fat_mes_vlr   = sum(v.get('REALIZADO', 0) for v in mi_d.values()) + ec_d.get('REALIZADO', 0)
    prev_mes_vlr  = sum(v.get('PREVISTO', 0) for v in mi_d.values()) + ec_d.get('PREVISTO', 0)
    fat_mes_pares = 0; prev_mes_pares = 0  # pares não armazenados na estrutura nova

    dados = {
        'gerado_em': agora.strftime('%d/%m/%Y %H:%M'),
        'meta_semanal': META_SEMANAL,
        'vendas': {
            'mes_atual': mes_atual, 'mes_label': mes_label,
            'canais_mes': vendas['canais_mes'],
            'tipos_mes': vendas.get('tipos_mes', {}),
            'total_mes': vendas['total_mes'],
            'pendente_validacao': vendas['pendente_validacao'],
        },
        'programacao': {
            'mes_label': mes_label,
            'total_mes': sum(v['total'] for v in sems_mes.values()),
            'semanas': sems_mes,
        },
        'estoque': estoque['totais'],
        'carteira': {
            'total_pedidos': carteira.get('total_pedidos', 0),
            'total_pares': carteira.get('total_pares', 0),
        },
        'faturamento': {
            'mes_label': mes_label,
            'fat_mes_vlr': round(fat_mes_vlr, 2),
            'prev_mes_vlr': round(prev_mes_vlr, 2),
            'fat_mes_pares': int(fat_mes_pares),
            'prev_mes_pares': int(prev_mes_pares),
        },
    }
    with open(os.path.join(output_dir, 'dados_portal.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados, f_, ensure_ascii=False, indent=2)
    print(f"\n  ✓ dados_portal.json atualizado")

def gerar_dados_completos(vendas, prog, estoque, carteira, output_dir='.'):
    dados = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'vendas': {
            'canais_mes': vendas['canais_mes'],
            'tipos_mes': vendas.get('tipos_mes', {}),
            'canais_total': vendas['canais_total'],
            'mensal': vendas['mensal'],
        },
        'programacao': {'semanas': prog['semanas']},
        'estoque': {'totais': estoque['totais'], 'refs': estoque['refs']},
        'carteira': {
            'total_pedidos': carteira.get('total_pedidos', 0),
            'total_pares': carteira.get('total_pares', 0),
            'canais': carteira.get('canais', {}),
        },
    }
    with open(os.path.join(output_dir, 'boaonda_dados_completos.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados, f_, ensure_ascii=False, default=str)
    print(f"  ✓ boaonda_dados_completos.json atualizado")

def _carregar_vendas_prog_existentes(output_dir):
    """Recarrega vendas/programação/carteira a partir dos JSONs já gerados,
    para não apagar esses dados do portal quando só o ESQT é reprocessado."""
    try:
        with open(os.path.join(output_dir, 'dados_vendas.json'), encoding='utf-8') as f_:
            dv = json.load(f_)
        vendas = {
            'canais_mes': dv.get('canais_mes', {}),
            'tipos_mes': dv.get('tipos_mes', {}),
            'canais_total': dv.get('canais_total', {}),
            'total_mes': dv.get('total_mes', 0),
            'mensal': dv.get('mensal', {}),
            'pendente_validacao': dv.get('pendente_validacao', True),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        vendas = {'canais_mes':{},'tipos_mes':{},'canais_total':{},'total_mes':0,'mensal':{},'pendente_validacao':True}

    try:
        with open(os.path.join(output_dir, 'dados_programacao.json'), encoding='utf-8') as f_:
            dp = json.load(f_)
        prog = {'semanas': dp.get('semanas', {})}
    except (FileNotFoundError, json.JSONDecodeError):
        prog = {'semanas': {}}

    try:
        with open(os.path.join(output_dir, 'dados_carteira.json'), encoding='utf-8') as f_:
            dc = json.load(f_)
        carteira = {
            'total_pedidos': dc.get('total_pedidos', 0),
            'total_pares': dc.get('total_pares', 0),
            'canais': dc.get('canais', {}),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        carteira = {'total_pedidos':0,'total_pares':0,'canais':{}}

    return vendas, prog, carteira

def _carregar_faturamento_existente(output_dir):
    try:
        with open(os.path.join(output_dir,'dados_faturamento.json'),encoding='utf-8') as f_:
            return json.load(f_)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'gerado_em':'','taxa_cambio_me':5.0,'dados':{},'sem_data':{}}

# ─── ORQUESTRADOR ────────────────────────────────────────────────────
def processar_tudo(arquivo_3ys=None, arquivo_esqt=None, output_dir='.'):
    """Roda o pipeline completo. Retorna um resumo (dict) do que foi gerado.

    arquivo_3ys / arquivo_esqt: caminhos para os arquivos de origem.
    Se arquivo_3ys for None, vendas/programação são puladas (mantém dados anteriores).
    output_dir: pasta onde os JSONs de saída são gravados.
    """
    agora = datetime.now()
    mes_atual = agora.strftime('%Y%m')
    m_nomes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho',
               'Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
    mes_label = f"{m_nomes[agora.month-1][:3]}/{agora.year}"

    if not arquivo_esqt or not os.path.exists(arquivo_esqt):
        raise FileNotFoundError("ESQT não encontrado.")

    estoque = processar_estoque(arquivo_esqt, output_dir)

    usar_mysql = bool(os.environ.get('MYSQL_HOST'))
    if usar_mysql or (arquivo_3ys and os.path.exists(arquivo_3ys)):
        linhas = carregar_linhas_3ys(arquivo_3ys)
        vendas   = processar_vendas(linhas, mes_atual, output_dir)
        processar_vendas_eva(linhas, mes_atual, output_dir)
        gerar_dados_vendas_clientes(linhas, output_dir)
        gerar_dados_vendas_carteira(linhas, output_dir)
        prog     = processar_programacao(linhas, output_dir)
        carteira = processar_carteira(linhas, output_dir)
        fat      = processar_faturamento(linhas, output_dir)
    else:
        vendas, prog, carteira = _carregar_vendas_prog_existentes(output_dir)
        fat = _carregar_faturamento_existente(output_dir)

    gerar_json_portal(vendas, prog, estoque, carteira, mes_atual, mes_label, output_dir, fat=fat)
    gerar_dados_completos(vendas, prog, estoque, carteira, output_dir)

    cm = vendas['canais_mes']
    t  = estoque['totais']
    return {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'mes_label': mes_label,
        'vendas_mes': cm,
        'estoque_totais': t,
        'diag_vendas': vendas.get('_diag', {}),
        'arquivos': ['dados_portal.json','dados_programacao.json','dados_programacao_detalhe.json',
                      'dados_refs_tabela.json','dados_vendas.json','dados_vendas_eva.json','dados_estoque.json',
                      'dados_vendas_clientes.json','dados_vendas_carteira.json',
                      'dados_carteira.json','dados_faturamento.json','boaonda_dados_completos.json'],
    }

# ─── CLI ─────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("  BOAONDA INTELLIGENCE — Atualização v1.2")
    print("=" * 52)

    arquivo_3ys = achar_3ys('.')
    arquivo_esqt = 'ESQT.xls'

    print(f"\n  Verificando arquivos...")
    usar_mysql = bool(os.environ.get('MYSQL_HOST'))
    tem_3ys  = os.path.exists(arquivo_3ys)
    tem_esqt = os.path.exists(arquivo_esqt)
    if usar_mysql:
        print(f"  ✓ MYSQL_HOST configurado — vendas/programação/carteira virão do MySQL")
    elif tem_3ys:
        print(f"  ✓ {arquivo_3ys} ({os.path.getsize(arquivo_3ys)/1024:.0f} KB)")
    else:
        print(f"  ⚠ Não encontrado: 3YS — vendas e programação não serão atualizadas")
    if tem_esqt:
        print(f"  ✓ {arquivo_esqt} ({os.path.getsize(arquivo_esqt)/1024:.0f} KB)")
    else:
        print(f"  ✗ Não encontrado: {arquivo_esqt}")

    if not tem_esqt:
        print("\n  ✗ ESQT.xls não encontrado. Corrija e tente novamente.\n")
        sys.exit(1)

    resumo = processar_tudo(
        arquivo_3ys if tem_3ys else None,
        arquivo_esqt,
        output_dir='.',
    )

    print("\n" + "=" * 52)
    print("  RESUMO")
    print("=" * 52)
    cm = resumo['vendas_mes']
    print(f"\n  VENDAS — {resumo['mes_label']}")
    print(f"    MI: {cm.get('MI',0):>8,} | ME: {cm.get('ME',0):>8,} | E-com: {cm.get('ECOM',0):>6,} | Mould: {cm.get('GRUPO_MOULD',0):>6,}")

    t = resumo['estoque_totais']
    print(f"\n  ESTOQUE PA")
    print(f"    Físico: {t['fisico']:>8,} | Reservas: {t['reservas']:>8,} | Livre: {t['livre']:>8,}")

    print(f"\n  Arquivos gerados:")
    for arq in resumo['arquivos']:
        print(f"    ✓ {arq}")
    print(f"\n  Recarregue o portal no browser (F5).")
    print("=" * 52 + "\n")

if __name__ == '__main__':
    main()
