"""
Boaonda Intelligence — Processador de Capacidade Fabril
========================================================
Lê a planilha "Programação_GERAL" (aba CAP_PROD DIA) e gera
dados_capacidade.json com a capacidade produtiva por referência/linha
(Camada 1), o pool de máquinas de injetados (Camada 2), o teto agregado
do atelier (Camada 0) e os setores de apoio (Palmilha, Sola, Pintura).

Bloco independente — sem cruzamento com vendas/estoque/programação
nesta fase, exceto a validação de cobertura (seção 4 da especificação),
que cruza as referências de `dados_refs_tabela.json` apenas para
medir cobertura, sem alterar os dados de capacidade.

Ver: ESPECIFICACAO_motor_capacidade.md

Entrada:
    - Programação_GERAL.xlsx, aba "CAP_PROD DIA", linhas 9+,
      colunas B-L (Camada 1) e AA/AB/AD/AF-AL (Camada 2)

Saída (gravada em output_dir):
    - dados_capacidade.json
"""

import json, os, sys
from datetime import datetime
from collections import Counter, defaultdict

# ─── CAMADA 0 — Teto do Atelier (semanal, agregado) ────────────────────
TETO_ATELIER_SEMANAL = {
    'convencional_pares_semana': 29000,
    'montado_pares_semana': 6000,
    'total_pares_semana': 35000,
    'observacao': (
        'Limite agregado da soma de TODAS as referências do mix programado '
        'na semana, independente das capacidades individuais por referência '
        '(Camada 1).'
    ),
}

# ─── CAMADA 2 — Pool de Máquinas de Injetados (config do parque) ───────
POOL_MAQUINAS_INJETADOS = {
    'horizontal': {'maquinas': 9, 'horas_dia': 8.48, 'min_dia_total': 4579.2},
    'tiras':      {'maquinas': 3, 'horas_dia': 8.48, 'min_dia_total': 1526.4},
    'rotativa':   {'maquinas': 1, 'horas_dia': 23,   'min_dia_total': 1380.0},
    'vertical':   {'maquinas': 1, 'horas_dia': 8.48, 'min_dia_total': 508.8},
    'eva': {
        'maquinas': 3, 'horas_dia': 24, 'min_dia_total': 4320.0,
        'maquinas_ativas': ['maq1', 'maq2', 'maq3'],
        'maquina_reserva': 'maq4 (não incluída na capacidade padrão)',
    },
}

EFICIENCIAS_PADRAO = {
    'horizontal': 0.60, 'tiras': 0.60, 'vertical': 0.55,
    'rotativa_outros': 0.50, 'rotativa_lily': 0.55, 'rotativa_nellie': 0.70,
    'eva_maq1': 0.80, 'eva_maq2': 0.75, 'eva_maq3': 0.75, 'eva_maq4': 1.0,
    'eva_maq4_observacao': (
        'Máquina reserva, NÃO incluída no cálculo padrão do pool EVA'
    ),
}

GIRO_DIAS = {
    'corte': 7, 'costura': 4, 'palmilha': 3,
    'observacao': 'Lead time em dias úteis antes da montagem (sequencial, não simultâneo)',
}

# ─── Mapeamento de colunas da aba CAP_PROD DIA (1-indexado, A=1) ───────
COL = {
    'ref_linha': 2, 'material': 3, 'referencia': 4, 'linha': 5,
    'descricao': 6, 'tipo_montagem': 7,
    'cap_mont1_conv': 8, 'cap_mont2_montado': 9,
    'cap_palmilha': 10, 'cap_sola': 11, 'cap_pintura': 12,
    # Camada 2 — tempos padrão do pool de injetados (min/par)
    'horiz_sola': 27, 'horiz_tiras': 28, 'vert_sola': 30,
    'rot_outros': 32, 'rot_lily': 33, 'rot_nellie': 34,
    'eva_maq1': 35, 'eva_maq2': 36, 'eva_maq3': 37, 'eva_maq4': 38,
}

# Coluna planilha -> grupo do pool de injetados
COLUNA_POOL = {
    'horiz_sola': 'horizontal', 'horiz_tiras': 'tiras', 'vert_sola': 'vertical',
    'rot_outros': 'rotativa', 'rot_lily': 'rotativa', 'rot_nellie': 'rotativa',
    'eva_maq1': 'eva', 'eva_maq2': 'eva', 'eva_maq3': 'eva', 'eva_maq4': 'eva',
}

LINHA_INICIO = 9
ABA_CAPACIDADE = 'CAP_PROD DIA'

# Capacidade observada (PRS/DIA) dos setores — aba ANALISE da planilha,
# levantamento de 12/06/2026. Apenas informativa (Bloco 1 do dashboard);
# atualizar manualmente se o usuário remedir esses valores.
CAPACIDADE_DIA_OBSERVADA = {
    'mont1_convencional': 7302.18,
    'mont2_montado': 1085.48,
    'filial_palmilha': 4394.84,
    'filial_sola': 3313.67,
    'pintura': 1197.31,
}


def _num(v):
    """Converte célula da planilha em float, tratando vazio/None."""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def processar_capacidade(caminho_planilha, output_dir='.'):
    print("\n  Processando capacidade fabril...")
    try:
        import openpyxl
    except ImportError:
        print("    ✗ openpyxl não instalado. Rode: pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.load_workbook(caminho_planilha, data_only=True, read_only=True)
    ws = wb[ABA_CAPACIDADE]

    referencias = {}
    por_tipo = Counter()
    palmilha_caps, sola_caps = set(), set()
    palmilha_refs = sola_refs = 0
    pintura_grupos = Counter()

    for row in ws.iter_rows(min_row=LINHA_INICIO, values_only=True):
        ref_linha = row[COL['ref_linha'] - 1]
        if ref_linha is None:
            continue
        ref_linha = str(ref_linha).strip()
        if not ref_linha:
            continue

        tipo_raw = str(row[COL['tipo_montagem'] - 1] or '').strip().upper()
        if 'CONVENCIONAL' in tipo_raw:
            tipo = 'CONVENCIONAL'
        elif 'MONTADO' in tipo_raw:
            tipo = 'MONTADO'
        else:
            tipo = None

        cap_conv = _num(row[COL['cap_mont1_conv'] - 1])
        cap_mont = _num(row[COL['cap_mont2_montado'] - 1])
        cap_montagem = cap_conv if tipo == 'CONVENCIONAL' else (cap_mont if tipo == 'MONTADO' else None)
        if not cap_montagem:
            continue  # só referências com capacidade de montagem definida (Camada 1)

        por_tipo[tipo] += 1

        cap_palmilha = _num(row[COL['cap_palmilha'] - 1])
        cap_sola = _num(row[COL['cap_sola'] - 1])
        cap_pintura = _num(row[COL['cap_pintura'] - 1])
        if cap_palmilha:
            palmilha_refs += 1
            palmilha_caps.add(int(cap_palmilha))
        if cap_sola:
            sola_refs += 1
            sola_caps.add(int(cap_sola))
        if cap_pintura:
            pintura_grupos[int(cap_pintura)] += 1

        tempos = defaultdict(list)
        for col_nome, pool in COLUNA_POOL.items():
            v = _num(row[COL[col_nome] - 1])
            if v:
                tempos[pool].append({'componente': col_nome, 'tempo_padrao_min': round(v, 2)})

        referencias[ref_linha] = {
            'material': str(row[COL['material'] - 1] or '').strip(),
            'referencia': str(row[COL['referencia'] - 1] or '').strip(),
            'linha': row[COL['linha'] - 1],
            'descricao': str(row[COL['descricao'] - 1] or '').strip(),
            'tipo_montagem': tipo,
            'capacidade_montagem_dia': int(cap_montagem),
            'capacidades_apoio': {
                'palmilha': int(cap_palmilha) if cap_palmilha else None,
                'sola': int(cap_sola) if cap_sola else None,
                'pintura': int(cap_pintura) if cap_pintura else None,
            },
            'tempos_padrao_pool_min': dict(tempos),
        }

    print(f"    {len(referencias)} referências com capacidade de montagem "
          f"({por_tipo.get('CONVENCIONAL', 0)} conv. + {por_tipo.get('MONTADO', 0)} montado)")

    setores_apoio = {
        'filial_palmilha': {
            'capacidade_dia': max(palmilha_caps) if palmilha_caps else 0,
            'refs_que_usam': palmilha_refs,
            'observacao': 'Capacidade do setor inteiro, idêntica em todas as refs que passam por ele',
        },
        'filial_sola': {
            'capacidade_dia': max(sola_caps) if sola_caps else 0,
            'refs_que_usam': sola_refs,
            'observacao': 'Capacidade do setor inteiro, idêntica em todas as refs que passam por ele',
        },
        'pintura': {
            'grupos': {str(k): v for k, v in sorted(pintura_grupos.items())},
            'total_refs': sum(pintura_grupos.values()),
            'observacao': 'Capacidade por linha/célula de pintura, transversal a CONVENCIONAL e MONTADO',
        },
        'giro_dias': GIRO_DIAS,
        'capacidade_dia_observada': CAPACIDADE_DIA_OBSERVADA,
    }

    metadados = {
        'total_referencias_cadastradas': sum(1 for row in ws.iter_rows(min_row=LINHA_INICIO, values_only=True)
                                              if row[COL['ref_linha'] - 1]),
        'total_com_capacidade_montagem': len(referencias),
        'por_tipo': dict(por_tipo),
    }

    validacao_cobertura = calcular_validacao_cobertura(referencias, output_dir)

    dados = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y'),
        'teto_atelier_semanal': TETO_ATELIER_SEMANAL,
        'pool_maquinas_injetados': POOL_MAQUINAS_INJETADOS,
        'eficiencias_padrao': EFICIENCIAS_PADRAO,
        'metadados': metadados,
        'referencias': referencias,
        'setores_apoio': setores_apoio,
        'validacao_cobertura': validacao_cobertura,
    }

    with open(os.path.join(output_dir, 'dados_capacidade.json'), 'w', encoding='utf-8') as f_:
        json.dump(dados, f_, ensure_ascii=False, default=str)
    print(f"    ✓ dados_capacidade.json gerado ({len(referencias)} referências)")
    return dados


def calcular_validacao_cobertura(referencias, output_dir):
    """Cruza as referências com capacidade cadastrada contra o volume real
    da programação (dados_refs_tabela.json) — única referência cruzada
    permitida nesta fase (seção 4 da especificação)."""
    refs_capacidade = {v['referencia'] for v in referencias.values()}

    pares_por_ref = Counter()
    try:
        with open(os.path.join(output_dir, 'dados_refs_tabela.json'), encoding='utf-8') as f_:
            rt = json.load(f_)
        for semana in (rt.get('semanas') or {}).values():
            for ref, qtd in (semana.get('refs') or {}).items():
                pares_por_ref[ref] += qtd
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    refs_programacao = set(pares_por_ref.keys())
    cobertas = refs_programacao & refs_capacidade
    nao_cobertas = refs_programacao - refs_capacidade
    total_pares = sum(pares_por_ref.values())
    pares_cobertos = sum(p for r, p in pares_por_ref.items() if r in refs_capacidade)

    return {
        'refs_programacao_unicas': len(refs_programacao),
        'refs_com_capacidade': len(cobertas),
        'total_pares_periodo': total_pares,
        'pares_cobertos': pares_cobertos,
        'pct_cobertura_volume': round(pares_cobertos / total_pares * 100, 1) if total_pares else None,
        'refs_sem_capacidade': sorted(
            ({'referencia': r, 'pares': pares_por_ref[r]} for r in nao_cobertas),
            key=lambda x: -x['pares']
        ),
    }


def achar_planilha_capacidade(diretorio='.'):
    """Procura a planilha 'Programação_GERAL' (.xlsx) na pasta indicada."""
    for nome in os.listdir(diretorio):
        nome_lower = nome.lower()
        if nome_lower.endswith('.xlsx') and 'program' in nome_lower and 'geral' in nome_lower:
            return os.path.join(diretorio, nome)
    return None


# ─── CLI ─────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("  BOAONDA INTELLIGENCE — Capacidade Fabril")
    print("=" * 52)

    caminho = achar_planilha_capacidade('.')
    if not caminho:
        print("\n  ✗ Planilha 'Programação_GERAL' (.xlsx) não encontrada na pasta atual.")
        print("    Coloque o arquivo aqui e rode novamente.\n")
        sys.exit(1)

    print(f"\n  ✓ {caminho} ({os.path.getsize(caminho)/1024:.0f} KB)")
    dados = processar_capacidade(caminho, output_dir='.')

    v = dados['validacao_cobertura']
    print("\n" + "=" * 52)
    print("  RESUMO")
    print("=" * 52)
    print(f"\n  {dados['metadados']['total_com_capacidade_montagem']} referências mapeadas "
          f"({dados['metadados']['por_tipo']})")
    if v.get('pct_cobertura_volume') is not None:
        print(f"  Cobertura da programação real: {v['pct_cobertura_volume']}% "
              f"({v['refs_com_capacidade']}/{v['refs_programacao_unicas']} refs)")
        if v['refs_sem_capacidade']:
            print(f"  Refs sem capacidade: {', '.join(r['referencia'] for r in v['refs_sem_capacidade'])}")
    print(f"\n  Recarregue o portal no browser (F5).")
    print("=" * 52 + "\n")


if __name__ == '__main__':
    main()
