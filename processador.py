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
    'pos_item':40,
}

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

def g(row, idx):
    try: return row[idx].strip()
    except: return ''

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

# ─── VENDAS ──────────────────────────────────────────────────────────
def processar_vendas(arquivo_3ys, mes_atual, output_dir='.'):
    print("\n  Processando vendas...")
    sep = detectar_sep(arquivo_3ys)
    canais_mes = {'MI':0,'ME':0,'ECOM':0,'GRUPO_MOULD':0}
    canais_total = {'MI':0,'ME':0,'ECOM':0,'GRUPO_MOULD':0}
    tipos_mes = {'MI':defaultdict(int),'ME':defaultdict(int),'ECOM':defaultdict(int)}
    mensal = defaultdict(lambda: {'MI':defaultdict(int),'ME':defaultdict(int),
                                   'ECOM':defaultdict(int),'GRUPO_MOULD':0})
    linha_canal = defaultdict(lambda: defaultdict(int))
    refs = Counter()
    refs_mi = Counter()
    refs_me = Counter()
    refs_ec = Counter()
    holdings = defaultdict(lambda: defaultdict(int))

    with open(arquivo_3ys, 'r', encoding='utf-8', errors='replace') as f_:
        reader = csv.reader(f_, delimiter=sep)
        next(reader)
        for row in reader:
            abr = g(row, IDX['abr_grp']).upper()
            cod = g(row, IDX['cod_esp'])
            pos_item = g(row, IDX['pos_item'])
            canal, tipo = classifica_venda(abr, cod, pos_item)
            if not canal: continue
            try: qtd = int(float(g(row, IDX['qtd']).replace(',','.')))
            except: qtd = 0
            if qtd <= 0: continue
            anomes = g(row, IDX['anomes'])
            ref = g(row, IDX['ref'])
            linha = g(row, IDX['linha'])
            ln = linha if linha in ('CLASSIC','EVA','WORKS','FIT','DAY BY DAY') else 'OUTROS'
            holding = g(row, IDX['nomeholder']) or g(row, IDX['razao'])[:40]

            canais_total[canal] += qtd
            if anomes == mes_atual:
                canais_mes[canal] += qtd
                if tipo: tipos_mes[canal][tipo] += qtd
            if anomes and len(anomes) == 6 and anomes.isdigit():
                if tipo: mensal[anomes][canal][tipo] += qtd
                else: mensal[anomes][canal] += qtd
            if ln != 'OUTROS':
                linha_canal[ln][canal] += qtd
            if ref:
                refs[ref] += qtd
                if canal == 'MI': refs_mi[ref] += qtd
                elif canal == 'ME': refs_me[ref] += qtd
                elif canal == 'ECOM': refs_ec[ref] += qtd
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

    # Gravar dados_vendas.json
    dados_vend = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'mes_atual': mes_atual,
        'canais_mes': canais_mes,
        'tipos_mes': tipos_mes_out,
        'canais_total': canais_total,
        'total_mes': total_mes,
        'mensal': mensal_out,
        'refs_top20': {
            'MI': refs_mi.most_common(20) if hasattr(refs_mi,'most_common') else [],
            'ME': refs_me.most_common(20) if hasattr(refs_me,'most_common') else [],
            'EC': refs_ec.most_common(20) if hasattr(refs_ec,'most_common') else [],
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

# ─── PROGRAMAÇÃO ─────────────────────────────────────────────────────
def processar_programacao(arquivo_3ys, output_dir='.'):
    print("\n  Processando programação...")
    sep = detectar_sep(arquivo_3ys)

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

    with open(arquivo_3ys, 'r', encoding='utf-8', errors='replace') as f_:
        reader = csv.reader(f_, delimiter=sep)
        next(reader)
        for row in reader:
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
            tp = g(row, 85).upper()
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

# ─── ESTOQUE ─────────────────────────────────────────────────────────
def processar_estoque(arquivo_esqt, output_dir='.'):
    print("\n  Processando estoque...")
    try: import xlrd
    except ImportError:
        print("    ✗ xlrd não instalado. Rode: pip install xlrd")
        sys.exit(1)

    wb = xlrd.open_workbook(arquivo_esqt)
    ws = wb.sheet_by_index(0)
    estoque = defaultdict(lambda: {
        'descricaoCompleta':'','fisico':0,'reservas':0,'livre':0,
        'vlr_fisico':0.0,'vlr_livre':0.0,'locais':set(),
        'combinacoes': defaultdict(lambda: {'fisico':0,'reservas':0,'livre':0})
    })

    for r in range(1, ws.nrows):
        def ge(c): return str(ws.cell_value(r,c)).strip()
        local = ge(1)
        if local.endswith('.0'): local = local[:-2]
        if local not in LOCAIS_ESTOQUE: continue
        ref = ge(2); descr = ge(3); comb = ge(5)
        try: fisico   = float(ge(8)) if ge(8) else 0
        except: fisico = 0
        try: reservas = float(ge(9)) if ge(9) else 0
        except: reservas = 0
        try: livre    = float(ge(10)) if ge(10) else 0
        except: livre = 0
        try: vlr_f   = float(ge(11)) if ge(11) else 0.0
        except: vlr_f = 0.0
        try: vlr_l   = float(ge(12)) if ge(12) else 0.0
        except: vlr_l = 0.0
        e = estoque[ref]
        if not e['descricaoCompleta'] and descr: e['descricaoCompleta'] = descr
        e['fisico'] += fisico; e['reservas'] += reservas; e['livre'] += livre
        e['vlr_fisico'] += vlr_f; e['vlr_livre'] += vlr_l; e['locais'].add(local)
        if comb:
            e['combinacoes'][comb]['fisico'] += fisico
            e['combinacoes'][comb]['reservas'] += reservas
            e['combinacoes'][comb]['livre'] += livre

    total_f = sum(d['fisico'] for d in estoque.values())
    total_r = sum(d['reservas'] for d in estoque.values())
    total_l = sum(d['livre'] for d in estoque.values())
    print(f"    Físico: {total_f:,.0f} | Reservas: {total_r:,.0f} | Livre: {total_l:,.0f}")

    estoque_out = {}
    for ref, d in sorted(estoque.items(), key=lambda x: -x[1]['livre']):
        estoque_out[ref] = {
            'descricaoCompleta':d['descricaoCompleta'],
            'fisico':int(d['fisico']),'reservas':int(d['reservas']),'livre':int(d['livre']),
            'vlr_fisico':round(d['vlr_fisico'],2),'vlr_livre':round(d['vlr_livre'],2),
            'locais':sorted(list(d['locais'])),'qtd_combinacoes':len(d['combinacoes']),
            'combinacoes':{k:{kk:int(vv) for kk,vv in v.items()}
                for k,v in sorted(d['combinacoes'].items(), key=lambda x:-x[1]['livre'])}
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

# ─── JSON PORTAL ─────────────────────────────────────────────────────
def gerar_json_portal(vendas, prog, estoque, mes_atual, mes_label, output_dir='.'):
    agora = datetime.now()
    ano = int(mes_atual[:4]); mes = int(mes_atual[4:])
    mes_ref_atual = f"{ano}-{mes:02d}"
    # Uma semana pertence ao mês cujo último dia útil (sexta-feira) ela contém
    # — ver 'mes_ref' calculado em processar_programacao.
    sems_mes = {k:v for k,v in prog['semanas'].items()
                if v.get('mes_ref')==mes_ref_atual}

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
    }
    with open(os.path.join(output_dir, 'dados_portal.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados, f_, ensure_ascii=False, indent=2)
    print(f"\n  ✓ dados_portal.json atualizado")

def gerar_dados_completos(vendas, prog, estoque, output_dir='.'):
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
    }
    with open(os.path.join(output_dir, 'boaonda_dados_completos.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados, f_, ensure_ascii=False, default=str)
    print(f"  ✓ boaonda_dados_completos.json atualizado")

def _carregar_vendas_prog_existentes(output_dir):
    """Recarrega vendas/programação a partir dos JSONs já gerados, para não
    apagar esses dados do portal quando só o ESQT é reprocessado."""
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

    return vendas, prog

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

    if arquivo_3ys and os.path.exists(arquivo_3ys):
        vendas = processar_vendas(arquivo_3ys, mes_atual, output_dir)
        prog   = processar_programacao(arquivo_3ys, output_dir)
    else:
        vendas, prog = _carregar_vendas_prog_existentes(output_dir)

    gerar_json_portal(vendas, prog, estoque, mes_atual, mes_label, output_dir)
    gerar_dados_completos(vendas, prog, estoque, output_dir)

    cm = vendas['canais_mes']
    t  = estoque['totais']
    return {
        'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        'mes_label': mes_label,
        'vendas_mes': cm,
        'estoque_totais': t,
        'arquivos': ['dados_portal.json','dados_programacao.json','dados_refs_tabela.json',
                      'dados_vendas.json','dados_estoque.json','boaonda_dados_completos.json'],
    }

# ─── CLI ─────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("  BOAONDA INTELLIGENCE — Atualização v1.2")
    print("=" * 52)

    arquivo_3ys = achar_3ys('.')
    arquivo_esqt = 'ESQT.xls'

    print(f"\n  Verificando arquivos...")
    tem_3ys  = os.path.exists(arquivo_3ys)
    tem_esqt = os.path.exists(arquivo_esqt)
    if tem_3ys:
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
