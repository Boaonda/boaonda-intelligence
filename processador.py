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

import csv, json, os, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

META_SEMANAL = 30000
LOCAIS_ESTOQUE = {'156','157','158','159','160','234','30','VI'}
GRUPOS_OK = ['MERCADO INTERNO','ISENTO','ECOMMERCE','E-COMMERCE','EXPORTA','MOULD']

IDX = {
    'razao':1,'ref':7,'descr':8,'forma':9,'qtd':10,
    'dt_ent':11,'dt_fat':12,'pedido':13,'anomes':18,
    'marca':20,'linha':23,'vlr':26,'cod_esp':27,'abr_grp':29,
    'holding':16,'nomeholder':17,'plano':35,'dt_plano':77,
    'pos_item':40,'etapa':39,'local':72,'tipomontagem':85,'cfop':71,
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
    marca           AS marca,
    {col_valor}     AS vlr
FROM mould.v_entradapedidos_extended v
WHERE v.dt_entrada >= date_format(date_sub(current_date, interval 1 year), '%Y/01/01')
"""

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
        db_rows = db_mysql.consultar(QUERY_3YS.format(col_valor=col_valor_sql))
        print(f"    {len(db_rows):,} linhas carregadas do MySQL")
        return [_linha_de_db_row(r) for r in db_rows]

    sep = detectar_sep(arquivo_3ys)
    linhas = []
    with open(arquivo_3ys, 'r', encoding='utf-8', errors='replace') as f_:
        reader = csv.reader(f_, delimiter=sep)
        next(reader)
        for row in reader:
            linhas.append(row)
    return linhas

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
    }

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

        if anomes == mes_atual:
            total_mes_kg += qtd_kg
            total_mes_valor += valor
        if anomes and len(anomes) == 6 and anomes.isdigit():
            mensal_kg[anomes] += qtd_kg
            mensal_valor[anomes] += valor
            if cliente:
                mc = mensal_clientes[anomes][cliente]
                mc[0] += qtd_kg; mc[1] += valor
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
                [{'cliente': c, 'kg': round(v[0], 1), 'valor': round(v[1], 2)} for c, v in clientes.items()],
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
                rows_norm.append((local, _g(2), _g(3), _g(5),
                                  parse_n(_g(8)),  parse_n(_g(9)),
                                  parse_n(_g(10)), parse_n(_g(11)), parse_n(_g(12))))
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
            rows_norm.append((local, ge(2), ge(3), ge(5),
                              parse_n(ge(8)),  parse_n(ge(9)),
                              parse_n(ge(10)), parse_n(ge(11)), parse_n(ge(12))))

    estoque = defaultdict(lambda: {
        'fisico':0,'reservas':0,'livre':0,'vlr_fisico':0.0,'vlr_livre':0.0,'locais':set(),
        'linhas': defaultdict(lambda: {
            'fisico':0,'reservas':0,'livre':0,'locais':set(),
            'combinacoes': defaultdict(lambda: {'fisico':0,'reservas':0,'livre':0})
        })
    })

    for local, ref, descr, comb, fisico, reservas, livre, vlr_f, vlr_l in rows_norm:
        if local not in LOCAIS_ESTOQUE: continue
        e = estoque[ref]
        e['fisico'] += fisico; e['reservas'] += reservas; e['livre'] += livre
        e['vlr_fisico'] += vlr_f; e['vlr_livre'] += vlr_l; e['locais'].add(local)
        if descr:
            ln = e['linhas'][descr]
            ln['fisico'] += fisico; ln['reservas'] += reservas; ln['livre'] += livre
            ln['locais'].add(local)
            if comb:
                ln['combinacoes'][comb]['fisico'] += fisico
                ln['combinacoes'][comb]['reservas'] += reservas
                ln['combinacoes'][comb]['livre'] += livre

    total_f = sum(d['fisico'] for d in estoque.values())
    total_r = sum(d['reservas'] for d in estoque.values())
    total_l = sum(d['livre'] for d in estoque.values())
    print(f"    Físico: {total_f:,.0f} | Reservas: {total_r:,.0f} | Livre: {total_l:,.0f}")

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
            'linhas': {
                descr: {
                    'fisico':int(ln['fisico']),'reservas':int(ln['reservas']),'livre':int(ln['livre']),
                    'locais':sorted(list(ln['locais'])),'qtd_combinacoes':len(ln['combinacoes']),
                    'combinacoes':{k:{kk:int(vv) for kk,vv in v.items()}
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
    # Gravar dados_estoque.json
    with open(os.path.join(output_dir, 'dados_estoque.json'), 'w', encoding='utf-8') as f_:
        json.dump({
            'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'totais': totais_est,
            'refs': estoque_out,
        }, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_estoque.json gerado ({len(estoque_out)} refs)")
    return {'refs': estoque_out, 'totais': totais_est}

# ─── FATURAMENTO ─────────────────────────────────────────────────────
ANOMESFATURA_COL = 19  # coluna anomesfatura no CSV 3YS (yyyymm da emissão NF)

def classifica_faturamento(cod, abr, marca=''):
    """Retorna (canal, tipo) para a área de Faturamento.

    EVA      → marca=='COMPOSTOS EVA' (checado primeiro, sem filtro de
               cliente/espécie — qualquer cod_esp/abr_grp conta)
    MI PROG  → cod=1   + (MERCADO INTERNO ou ISENTO em abr_grp)
    MI PE    → cod=22  + (MERCADO INTERNO ou ISENTO em abr_grp)
    MI MISTA → cod=31  + (MERCADO INTERNO ou ISENTO em abr_grp)
    ME       → cod=17 OU 'EXPORTA' em abr_grp, sem 'MOULD'
    EC       → cod=32 OU 'ECOMMERCE'/'E-COMMERCE' em abr_grp
    """
    if is_composto_eva(marca):
        return 'EVA', 'EVA'

    is_ec_abr  = 'ECOMMERCE' in abr or 'E-COMMERCE' in abr
    is_exporta = 'EXPORTA' in abr and 'MOULD' not in abr
    is_mi_abr  = ('MERCADO INTERNO' in abr or 'ISENTO' in abr) and 'EXPORTA' not in abr

    if cod == '32' or is_ec_abr:        return 'EC', 'EC'
    if cod == '17' or is_exporta:       return 'ME', 'ME'
    if is_mi_abr:
        if cod == '1':  return 'MI', 'PROG'
        if cod == '22': return 'MI', 'PE'
        if cod == '31': return 'MI', 'MISTA'
    return None, None

def processar_faturamento(linhas, output_dir='.', taxa_cambio_me=5.0):
    """Gera dados_faturamento.json — faturamento realizado e previsto por
    canal/espécie e mês de referência, com suporte a taxa de câmbio ME."""
    print("\n  Processando faturamento...")

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
    sem_data_list = []   # [{ref, canal, especie, pares, valor}]
    total_fat = total_prev = sem_data_count = 0

    for row in linhas:
        pos = g(row, IDX['pos_item']).strip().upper()
        if pos == 'CANCELADO': continue
        if pos == 'FATURADO':                                   status = 'fat'
        elif pos in ('NADA FATURADO','PARCIALMENTE FATURADO'):  status = 'prev'
        else: continue

        abr = g(row, IDX['abr_grp']).upper()
        cod = g(row, IDX['cod_esp'])
        marca = g(row, IDX['marca'])
        canal, tipo = classifica_faturamento(cod, abr, marca)
        if not canal: continue

        try: qtd_raw = float(g(row, IDX['qtd']).replace(',','.'))
        except: qtd_raw = 0.0
        if qtd_raw <= 0: continue
        qtd = qtd_raw if canal == 'EVA' else int(qtd_raw)  # EVA é kg (float), demais são pares

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

        m = dados[mes_ref]
        vi, qi = (0, 2) if status == 'fat' else (1, 3)
        if canal == 'MI':
            m['MI'][tipo][vi] += vlr; m['MI'][tipo][qi] += qtd
        elif canal == 'ME':
            m['ME'][vi] += vlr; m['ME'][qi] += qtd     # USD
        elif canal == 'EVA':
            m['EVA'][vi] += vlr; m['EVA'][qi] += qtd   # kg
        else:
            m['EC'][vi] += vlr; m['EC'][qi] += qtd

        # Acumular por CFOP — valor (BRL/USD) soma todos os canais; a
        # quantidade vai para pares (MI/ME/EC) ou kg (EVA), nunca somadas.
        cfop_code = g(row, IDX['cfop'])
        if cfop_code:
            dc = dados_cfop[mes_ref][cfop_code]
            vi_c = (2 if canal == 'ME' else 0) + (0 if status == 'fat' else 1)
            dc[vi_c] += vlr
            if canal == 'EVA':
                qi_c = 6 if status == 'fat' else 7
            else:
                qi_c = 4 if status == 'fat' else 5
            dc[qi_c] += qtd

        if status == 'fat':  total_fat  += qtd
        else:                total_prev += qtd

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
                out[cfop_code] = entry
        return out

    dados_cfop_out = {k: build_cfop_mes(v) for k, v in sorted(dados_cfop.items())}

    result = {'gerado_em':datetime.now().strftime('%d/%m/%Y %H:%M'),
              'taxa_cambio_me':taxa_cambio_me, 'dados':dados_out,
              'dados_cfop':dados_cfop_out, 'sem_data':sem_data_out}
    with open(os.path.join(output_dir,'dados_faturamento.json'),'w',encoding='utf-8') as f_:
        json.dump(result, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_faturamento.json gerado ({len(dados_out)} meses)")
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
        'arquivos': ['dados_portal.json','dados_programacao.json','dados_programacao_detalhe.json',
                      'dados_refs_tabela.json','dados_vendas.json','dados_vendas_eva.json','dados_estoque.json',
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
