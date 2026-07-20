"""
Boaonda Intelligence — Processador de Metas Comerciais
=======================================================
Metas mensais de venda por representante (mercado interno), em PARES.

Fluxo (round-trip de Excel, mesmo padrão da Capacidade Fabril):

    Portal gera Excel pré-preenchido (meta global + sugestão ponderada)
      →  gestor edita offline (fixa reps, ajusta)
      →  importa  →  valida (formato + faixas)  →  backup do JSON  →  grava

A "sugestão" distribui a meta global do mês entre os representantes de forma
top-down, ponderando o histórico de cada um (regra do Gestor Comercial):
crescimento 1.5x, estável 1.0x, declínio 0.6x — sempre comparando com o
MESMO mês do ano anterior (deseasonaliza), nunca com o mês imediatamente
anterior. Reps marcados como "fixar" travam num número; o restante da meta
global se redistribui entre os não-fixados ("o resto se ajusta ao redor").

Fonte de realizado: dados_vendas_carteira.json (mesma base dos painéis de
Vendas — holdings × mês × espécie MI). Higiene idêntica ao frontend
(REPS_EXCLUIDOS / REP_UNIFICAR / apenas MI_PROG|PE|MISTA).

Saída: dados_metas.json
    {
      "gerado_em": "20/07/2026",
      "meta_global": { "202607": 150000 },
      "metas":       { "202607": { "CLEOBERTO ...": 12000, ... } },
      "detalhe":     { "202607": { "CLEOBERTO ...": {"tendencia": "...",
                                    "peso": 1.5, "fixado": true}, ... } }
    }
"""

import json, os, sys
from datetime import datetime
from collections import defaultdict

# ─── Higiene de representante (espelho de boaonda_carteira_representante.html) ─
REPS_EXCLUIDOS = {
    'ATEND. COMERCIAL - OUTRAS MARCAS',
    'ECOMMERCE',
}
REP_UNIFICAR = {
    'ALEXSHOES REPRESENTACOES LTDA':   'FRALDA & COMPANHIA LTDA',
    'ROGERIO RAPPA SILVEIRA - ME':     'RRAPPA REPRESENTACOES LTDA',
    'RIBEIRO UDI REPRESENTACOES LTDA': 'CLEOBERTO REPRESENTACOES LTDA',
}
# Só estas 3 espécies contam como MI válido (Programado/Pronta Entrega/Mista);
# exclui MI_ECOM (resíduo) e todos os canais ECOM_*/ME_*.
TIPOS_MI_VALIDOS = ['PROG', 'PE', 'MISTA']

# ─── Parâmetros de classificação (calibráveis) ────────────────────────────
PESO_TREND = {'crescimento': 1.5, 'estavel': 1.0, 'declinio': 0.6, 'novo': 1.0}
LIMIAR_CRESCIMENTO = 0.10   # > +10% vs mesmo período ano anterior
LIMIAR_DECLINIO    = -0.10  # < -10% vs mesmo período ano anterior
JANELA_REF_MESES   = 3      # nº de meses fechados usados como referência recente

_CORES = {  # MIV Boaonda
    'header_bg': '1C2030', 'header_ft': 'F3F0EB',
    'zebra_a': 'F5F5F5', 'zebra_b': 'F9F9F9', 'coral': 'ED6842',
    'travado_bg': 'FCE9E2',
}

# ─── Aritmética de competência (AAAAMM) ───────────────────────────────────
def mes_menos(aaaamm, n):
    """Subtrai n meses de uma competência 'AAAAMM'. Retorna 'AAAAMM'."""
    a, m = int(str(aaaamm)[:4]), int(str(aaaamm)[4:])
    total = a * 12 + (m - 1) - n
    return f'{total // 12:04d}{total % 12 + 1:02d}'


def mes_label(aaaamm):
    meses = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun',
             'jul', 'ago', 'set', 'out', 'nov', 'dez']
    s = str(aaaamm)
    return f'{meses[int(s[4:]) - 1]}/{s[:4]}'


# ─── Índice de realizado por representante ────────────────────────────────
def _carregar_carteira(carteira_path):
    with open(carteira_path, encoding='utf-8') as f:
        return json.load(f)


def indice_reps(carteira):
    """Constrói, a partir de dados_vendas_carteira.json:
        vol[rep][mes]    = pares MI válidos (soma das holdings do rep)
        ativos[rep][mes] = nº de holdings com pares > 0 no mês
        reps             = set de representantes normalizados (pós-higiene)
    """
    meta = carteira.get('meta', {})
    holdings = carteira.get('holdings', {})
    vol = defaultdict(lambda: defaultdict(float))
    ativos = defaultdict(lambda: defaultdict(int))
    reps = set()

    for h, hmeta in meta.items():
        r = (hmeta.get('representantes') or {}).get('MI')
        if not r or r in REPS_EXCLUIDOS:
            continue
        rep = REP_UNIFICAR.get(r, r)
        if rep in REPS_EXCLUIDOS:
            continue
        reps.add(rep)
        for mes, tipos in (holdings.get(h) or {}).items():
            tot = 0.0
            for t in TIPOS_MI_VALIDOS:
                par = tipos.get('MI_' + t)
                if par:
                    tot += par[0]
            if tot > 0:
                vol[rep][mes] += tot
                ativos[rep][mes] += 1
    return vol, ativos, reps


def _vol(vol, rep, meses):
    return sum(vol[rep].get(m, 0) for m in meses)


def _meses_fechados_antes(meses_disp, competencia, n=JANELA_REF_MESES):
    """Últimos n meses disponíveis estritamente anteriores à competência."""
    ant = sorted(m for m in meses_disp if m < competencia)
    return ant[-n:]


def classificar_rep(vol, rep, meses_disp, competencia):
    """Classifica a tendência do rep comparando os últimos meses fechados com
    o mesmo período do ano anterior. Retorna dict com tendencia/peso/base/…"""
    ref = _meses_fechados_antes(meses_disp, competencia)
    recente = _vol(vol, rep, ref)
    ref_ya = [mes_menos(m, 12) for m in ref]
    recente_ya = _vol(vol, rep, ref_ya)

    if recente_ya <= 0:
        tendencia = 'novo' if recente > 0 else 'novo'
        cresc = None
    else:
        cresc = (recente - recente_ya) / recente_ya
        if cresc > LIMIAR_CRESCIMENTO:
            tendencia = 'crescimento'
        elif cresc < LIMIAR_DECLINIO:
            tendencia = 'declinio'
        else:
            tendencia = 'estavel'

    # Base sazonalmente correta = mesmo mês do ano anterior; se 0, média
    # mensal dos últimos meses fechados (fallback para reps sem YoY do mês).
    base_yoy = _vol(vol, rep, [mes_menos(competencia, 12)])
    base = base_yoy if base_yoy > 0 else (recente / len(ref) if ref else 0)

    return {
        'tendencia': tendencia,
        'peso': PESO_TREND[tendencia],
        'realizado_3m': int(round(recente)),
        'base_yoy': int(round(base_yoy)),
        'base': base,
        'crescimento_yoy': cresc,
    }


def sugerir_distribuicao(carteira, competencia, meta_global, fixados=None):
    """Distribui meta_global (pares) entre os reps. `fixados` é um dict
    rep→pares travado; o restante (meta_global − Σ fixados) é rateado entre os
    não-fixados proporcionalmente a base×peso. Retorna (metas, detalhe)."""
    fixados = {k: v for k, v in (fixados or {}).items() if v is not None}
    vol, _ativos, reps = indice_reps(carteira)
    meses_disp = carteira.get('meses_disponiveis', [])

    info = {rep: classificar_rep(vol, rep, meses_disp, competencia) for rep in reps}

    flex = [r for r in reps if r not in fixados]
    soma_fix = sum(fixados.get(r, 0) for r in fixados)
    restante = max(0, meta_global - soma_fix)

    peso_total = sum(info[r]['base'] * info[r]['peso'] for r in flex)

    metas, detalhe = {}, {}
    for rep in sorted(reps):
        if rep in fixados:
            pares = int(round(fixados[rep]))
            fixado = True
        elif peso_total > 0:
            w = info[rep]['base'] * info[rep]['peso']
            pares = int(round(restante * w / peso_total))
            fixado = False
        else:
            pares = 0
            fixado = False
        metas[rep] = pares
        detalhe[rep] = {
            'tendencia': info[rep]['tendencia'],
            'peso': info[rep]['peso'],
            'realizado_3m': info[rep]['realizado_3m'],
            'base_yoy': info[rep]['base_yoy'],
            'fixado': fixado,
        }

    # Reconcilia o arredondamento no maior rep flexível para o total fechar
    # exatamente na meta global (promessa do LEIA-ME).
    if flex:
        soma_flex = sum(metas[r] for r in flex)
        diff = restante - soma_flex
        if diff:
            maior = max(flex, key=lambda r: metas[r])
            metas[maior] = max(0, metas[maior] + diff)
    return metas, detalhe


# ─── Estilos de célula (openpyxl) ─────────────────────────────────────────
def _cel(ws, row, col, valor, bold=False, bg=None, ft='000000',
         align='left', size=10, wrap=False):
    from openpyxl.styles import Font, PatternFill, Alignment
    c = ws.cell(row=row, column=col, value=valor)
    c.font = Font(bold=bold, color=ft, name='Calibri', size=size)
    if bg:
        c.fill = PatternFill('solid', fgColor=bg)
    c.alignment = Alignment(horizontal=align, vertical='center', wrap_text=wrap)
    return c


def _header(ws, row, valores):
    for col, v in enumerate(valores, 1):
        _cel(ws, row, col, v, bold=True, bg=_CORES['header_bg'],
             ft=_CORES['header_ft'], align='center', wrap=True)


# ─── Abas do Excel ────────────────────────────────────────────────────────
_COLS_METAS = [
    'rep', 'realizado_3m', 'mesmo_mes_ano_passado', 'tendencia',
    'peso', 'clientes_ativos', 'sugestao_pares', 'fixar', 'meta_pares',
]
_COLS_OBRIG = ['rep', 'fixar', 'meta_pares']
_TEND_LABEL = {'crescimento': 'Crescimento', 'estavel': 'Estável',
               'declinio': 'Declínio', 'novo': 'Novo/sem histórico'}


def _sheet_leia_me(wb, competencia, meta_global):
    ws = wb.create_sheet('LEIA-ME')
    ws.column_dimensions['A'].width = 118
    linhas = [
        ('BOAONDA INTELLIGENCE — Metas Comerciais por Representante', True),
        (f'Competência: {mes_label(competencia)}   ·   Meta global: '
         f'{meta_global:,} pares'.replace(',', '.'), False),
        ('', False),
        ('COMO USAR', True),
        ('1. A aba PARÂMETROS traz a competência (mês) e a meta global de pares. '
         'Ajuste a meta global se quiser e reimporte para recalcular a sugestão.', False),
        ('2. Na aba METAS, cada linha é um representante. As colunas cinza são de '
         'apoio (só leitura) para você decidir com base em dado real.', False),
        ('3. Para TRAVAR a meta de um rep num valor específico: escreva "S" na '
         'coluna "fixar" e o número desejado em "meta_pares".', False),
        ('4. Os reps SEM "S" em "fixar" têm a meta recalculada automaticamente na '
         'importação — o restante da meta global (após os fixados) é redistribuído '
         'entre eles conforme o histórico (o resto se ajusta ao redor).', False),
        ('5. Salve e importe pelo portal (botão "Importar planilha" na tela de '
         'Metas Comerciais). O total sempre fecha na meta global.', False),
        ('', False),
        ('COLUNAS DA ABA METAS', True),
        ('  rep*                   — representante (não altere; é a chave)  OBRIGATÓRIO', False),
        ('  realizado_3m           — pares vendidos nos últimos 3 meses fechados (apoio)', False),
        ('  mesmo_mes_ano_passado  — pares no mesmo mês do ano anterior (base sazonal)', False),
        ('  tendencia              — Crescimento / Estável / Declínio (vs. ano anterior)', False),
        ('  peso                   — 1,5 crescimento · 1,0 estável · 0,6 declínio', False),
        ('  clientes_ativos        — nº de clientes com compra no último mês fechado', False),
        ('  sugestao_pares         — sugestão do sistema para a meta global informada', False),
        ('  fixar*                 — "S" trava a meta deste rep; vazio = recalculado  OBRIGATÓRIO', False),
        ('  meta_pares*            — meta em pares (usada quando fixar = "S")  OBRIGATÓRIO', False),
        ('', False),
        ('REGRAS DE VALIDAÇÃO', True),
        ('  meta global: inteiro > 0', False),
        ('  competência: AAAAMM (ex.: 202607)', False),
        ('  meta_pares: inteiro ≥ 0', False),
        ('  soma dos reps fixados não pode exceder a meta global', False),
        ('', False),
        ('METODOLOGIA (Gestor Comercial Boaonda)', True),
        ('  Base = mesmo mês do ano anterior (deseasonaliza). Tendência compara os '
         'últimos 3 meses fechados com o mesmo trimestre do ano anterior.', False),
        ('  Filtros aplicados: mercado interno, espécies Programado/Pronta Entrega/'
         'Mista; exclui e-commerce, EVA e exportação.', False),
    ]
    for i, (txt, bold) in enumerate(linhas, 1):
        _cel(ws, i, 1, txt, bold=bold,
             bg=_CORES['header_bg'] if bold else None,
             ft=_CORES['header_ft'] if bold else '000000',
             size=11 if bold else 10, wrap=True)


def _sheet_parametros(wb, competencia, meta_global):
    ws = wb.create_sheet('PARÂMETROS')
    ws.column_dimensions['A'].width = 24
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 46
    _header(ws, 1, ['CHAVE', 'VALOR', 'DESCRIÇÃO'])
    linhas = [
        ('competencia', int(competencia), 'Mês da meta no formato AAAAMM (ex.: 202607)'),
        ('meta_global', int(meta_global), 'Meta total de pares do mês (será distribuída)'),
    ]
    for i, (k, v, d) in enumerate(linhas, 2):
        _cel(ws, i, 1, k)
        _cel(ws, i, 2, v, align='center')
        _cel(ws, i, 3, d)
    ws.freeze_panes = 'A2'


def _sheet_metas(wb, carteira, competencia, meta_global, metas_atuais=None):
    ws = wb.create_sheet('METAS')
    larguras = [34, 14, 20, 14, 8, 14, 14, 8, 12]
    for col, w in enumerate(larguras, 1):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = w
    _header(ws, 1, _COLS_METAS)

    vol, ativos, reps = indice_reps(carteira)
    meses_disp = carteira.get('meses_disponiveis', [])
    ref_yoy = mes_menos(competencia, 12)
    ult_fechado = (_meses_fechados_antes(meses_disp, competencia) or [None])[-1]

    # Sugestão pura (sem fixados) para a meta global informada.
    metas_atuais = metas_atuais or {}
    fixados_ini = {r: metas_atuais[r] for r in metas_atuais} if metas_atuais else None
    sugestao, _det = sugerir_distribuicao(carteira, competencia, meta_global)

    for i, rep in enumerate(sorted(reps), 2):
        info = classificar_rep(vol, rep, meses_disp, competencia)
        cli_at = ativos[rep].get(ult_fechado, 0) if ult_fechado else 0
        yoy_pares = int(round(_vol(vol, rep, [ref_yoy])))
        bg = _CORES['zebra_a'] if i % 2 == 0 else None
        fixado = rep in metas_atuais
        meta_pre = metas_atuais.get(rep, sugestao.get(rep, 0))
        row = [
            (rep, 'left', False),
            (info['realizado_3m'], 'center', True),
            (f'{mes_label(ref_yoy)}: {yoy_pares:,}'.replace(',', '.'), 'center', True),
            (_TEND_LABEL[info['tendencia']], 'center', True),
            (info['peso'], 'center', True),
            (cli_at, 'center', True),
            (sugestao.get(rep, 0), 'center', True),
            ('S' if fixado else '', 'center', False),
            (int(round(meta_pre)), 'center', False),
        ]
        for col, (val, align, apoio) in enumerate(row, 1):
            cell_bg = _CORES['zebra_b'] if apoio else (
                _CORES['travado_bg'] if (col >= 8 and fixado) else bg)
            _cel(ws, i, col, val, bg=cell_bg, align=align,
                 ft='6b6b6b' if apoio else '000000')
    ws.freeze_panes = 'B2'


# ─── Exportar ─────────────────────────────────────────────────────────────
def exportar_metas_excel(dados_metas_path, carteira_path, competencia, meta_global):
    """Gera o Excel de metas (BytesIO). Se já houver metas gravadas para a
    competência, elas voltam pré-preenchidas (com fixar = "S")."""
    from io import BytesIO
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError('openpyxl não instalado. Rode: pip install openpyxl')

    competencia = str(competencia)
    meta_global = int(meta_global)
    carteira = _carregar_carteira(carteira_path)

    metas_atuais = {}
    if os.path.exists(dados_metas_path):
        with open(dados_metas_path, encoding='utf-8') as f:
            d = json.load(f)
        metas_atuais = (d.get('metas') or {}).get(competencia, {}) or {}
        if not meta_global:
            meta_global = int((d.get('meta_global') or {}).get(competencia, 0))

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _sheet_leia_me(wb, competencia, meta_global)
    _sheet_parametros(wb, competencia, meta_global)
    _sheet_metas(wb, carteira, competencia, meta_global, metas_atuais)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ─── Importar ─────────────────────────────────────────────────────────────
def _norm_header(v):
    s = str(v or '').strip().lower()
    return s.split('\n')[0].rstrip('*').strip()


def _achar_header_metas(ws):
    linhas = list(ws.iter_rows(max_row=10))
    for r_idx, row in enumerate(linhas, 1):
        header = [_norm_header(c.value) for c in row]
        if 'rep' in header and 'meta_pares' in header:
            return r_idx, header
    return 1, ([_norm_header(c.value) for c in linhas[0]] if linhas else [])


def _ler_parametros(ws):
    params = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[0] and str(row[0]).strip():
            params[str(row[0]).strip()] = row[1]
    return params


def _validar(wb):
    """Camadas 1 (formato) e 2 (faixas). Retorna lista de erros."""
    erros = []
    for sheet in ('PARÂMETROS', 'METAS'):
        if sheet not in wb.sheetnames:
            erros.append(f'Aba "{sheet}" não encontrada.')
    if erros:
        return erros

    params = _ler_parametros(wb['PARÂMETROS'])
    comp = str(params.get('competencia', '')).strip()
    if not (comp.isdigit() and len(comp) == 6):
        erros.append(f'PARÂMETROS › competencia inválida: "{comp}" (esperado AAAAMM)')
    try:
        mg = int(float(params.get('meta_global')))
        if mg <= 0:
            erros.append(f'PARÂMETROS › meta_global deve ser > 0 (recebido {mg})')
    except (TypeError, ValueError):
        erros.append(f'PARÂMETROS › meta_global inválida: "{params.get("meta_global")}"')
        mg = 0

    ws = wb['METAS']
    header_row, header = _achar_header_metas(ws)
    for col in _COLS_OBRIG:
        if col not in header:
            erros.append(f'Coluna obrigatória ausente em METAS: {col}')
    if erros:
        return erros

    idx = {n: i for i, n in enumerate(header)}
    soma_fix = 0
    for n_row, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True),
                                header_row + 1):
        if not row or row[idx['rep']] is None or not str(row[idx['rep']]).strip():
            continue
        rep = str(row[idx['rep']]).strip()
        fixar = str(row[idx['fixar']] or '').strip().upper()
        raw = row[idx['meta_pares']]
        if raw in (None, ''):
            mp = 0
        else:
            try:
                mp = float(raw)
            except (TypeError, ValueError):
                erros.append(f'METAS › linha {n_row} ({rep}) › meta_pares não numérico: "{raw}"')
                continue
        if mp < 0:
            erros.append(f'METAS › linha {n_row} ({rep}) › meta_pares negativo: {mp}')
        if fixar in ('S', 'SIM', '1', 'TRUE', 'VERDADEIRO', 'X'):
            soma_fix += mp
    if not erros and mg and soma_fix > mg:
        erros.append(f'Soma dos reps fixados ({int(soma_fix):,}'.replace(',', '.') +
                     f') excede a meta global ({mg:,})'.replace(',', '.') + '.')
    return erros


def importar_metas_excel(path_excel, dados_metas_path, carteira_path, output_dir=None):
    """Importa o Excel, recalcula a distribuição (fixados + redistribuição) e
    grava dados_metas.json (com backup). Retorna dict status/detalhes."""
    try:
        import openpyxl
    except ImportError:
        return {'status': 'erro', 'mensagem': 'openpyxl não instalado.'}

    if output_dir is None:
        output_dir = os.path.dirname(dados_metas_path)

    wb = openpyxl.load_workbook(path_excel, data_only=True)

    erros = _validar(wb)
    if erros:
        return {'status': 'erro', 'mensagem': 'Falha na validação:\n' + '\n'.join(erros)}

    params = _ler_parametros(wb['PARÂMETROS'])
    competencia = str(params['competencia']).strip()
    meta_global = int(float(params['meta_global']))

    ws = wb['METAS']
    header_row, header = _achar_header_metas(ws)
    idx = {n: i for i, n in enumerate(header)}
    fixados = {}
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if not row or row[idx['rep']] is None or not str(row[idx['rep']]).strip():
            continue
        rep = str(row[idx['rep']]).strip()
        fixar = str(row[idx['fixar']] or '').strip().upper()
        if fixar in ('S', 'SIM', '1', 'TRUE', 'VERDADEIRO', 'X'):
            raw = row[idx['meta_pares']]
            try:
                fixados[rep] = int(round(float(raw))) if raw not in (None, '') else 0
            except (TypeError, ValueError):
                fixados[rep] = 0

    carteira = _carregar_carteira(carteira_path)
    metas, detalhe = sugerir_distribuicao(carteira, competencia, meta_global, fixados)

    # Backup + merge da competência no JSON existente
    dados = {'meta_global': {}, 'metas': {}, 'detalhe': {}}
    if os.path.exists(dados_metas_path):
        with open(dados_metas_path, encoding='utf-8') as f:
            dados = json.load(f)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        with open(os.path.join(output_dir, f'dados_metas_backup_{ts}.json'),
                  'w', encoding='utf-8') as f:
            json.dump(dados, f, ensure_ascii=False, default=str)

    dados.setdefault('meta_global', {})[competencia] = meta_global
    dados.setdefault('metas', {})[competencia] = metas
    dados.setdefault('detalhe', {})[competencia] = detalhe
    dados['gerado_em'] = datetime.now().strftime('%d/%m/%Y')

    with open(dados_metas_path, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, default=str)

    n_fix = sum(1 for r in detalhe.values() if r['fixado'])
    return {
        'status': 'ok',
        'competencia': competencia,
        'competencia_label': mes_label(competencia),
        'meta_global': meta_global,
        'reps': len(metas),
        'reps_fixados': n_fix,
        'total_distribuido': sum(metas.values()),
        'gerado_em': dados['gerado_em'],
    }


# ─── CLI (teste rápido) ───────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print('uso: python processador_metas.py <competencia AAAAMM> <meta_global> '
              '[dir=frontend]')
        sys.exit(1)
    competencia, meta_global = sys.argv[1], int(sys.argv[2])
    d = sys.argv[3] if len(sys.argv) > 3 else 'frontend'
    carteira_path = os.path.join(d, 'dados_vendas_carteira.json')
    carteira = _carregar_carteira(carteira_path)
    metas, detalhe = sugerir_distribuicao(carteira, competencia, meta_global)
    print(f'\n  Sugestão de metas — {mes_label(competencia)} — '
          f'meta global {meta_global:,} pares\n'.replace(',', '.'))
    print(f'  {"REPRESENTANTE":<36}{"TEND":<14}{"PESO":>5}{"META":>10}')
    print('  ' + '-' * 66)
    for rep in sorted(metas, key=lambda r: -metas[r]):
        dd = detalhe[rep]
        print(f'  {rep[:34]:<36}{_TEND_LABEL[dd["tendencia"]][:12]:<14}'
              f'{dd["peso"]:>5}{metas[rep]:>10,}'.replace(',', '.'))
    print('  ' + '-' * 66)
    print(f'  {"TOTAL":<55}{sum(metas.values()):>10,}'.replace(',', '.'))


if __name__ == '__main__':
    main()
