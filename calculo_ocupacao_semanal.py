"""Calcula ocupação e eficiência semanal cruzando o mix de referências
programadas (dados_programacao_detalhe.json) com os tetos de capacidade
mapeados (dados_capacidade.json), gerando dados_ocupacao_semanal.json.

Nenhum dos dois arquivos de origem é modificado — este módulo apenas lê
ambos e expõe os indicadores (ver
"capacidade fabril/ESPECIFICACAO_ocupacao_eficiencia_semanal.md").
"""
import calendar
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), 'frontend')

DIAS_UTEIS_SEMANA = 5


def _carregar_json(nome):
    with open(os.path.join(FRONTEND_DIR, nome), 'r', encoding='utf-8') as f:
        return json.load(f)


def _montar_perfis_referencia(cap):
    """Agrega as múltiplas linhas/moldes cadastrados de cada referência
    num único perfil de consumo (a programação real não informa qual
    linha/molde específico foi usado, então não é possível cruzar por
    linha — ver nota de implementação da seção 3 da especificação):

    - tipo_montagem: o mais comum (moda) entre as linhas da referência
    - tempos_padrao_pool_min: média do total de min/par de cada pool,
      considerando apenas as linhas que usam aquele pool
    - setores de apoio (palmilha/sola/pintura): usa o setor se a MAIORIA
      das linhas da referência usar (moda; empate conta como "não usa")
    """
    por_ref = defaultdict(list)
    for linha in cap['referencias'].values():
        por_ref[linha['referencia']].append(linha)

    perfis = {}
    for ref, linhas in por_ref.items():
        tipos = Counter(l['tipo_montagem'] for l in linhas)
        tipo_montagem = tipos.most_common(1)[0][0]

        pools_tempos = defaultdict(list)
        for l in linhas:
            for pool, componentes in l.get('tempos_padrao_pool_min', {}).items():
                total_min = sum(c['tempo_padrao_min'] for c in componentes)
                pools_tempos[pool].append(total_min)
        pools_min_por_par = {pool: sum(tempos) / len(tempos)
                              for pool, tempos in pools_tempos.items()}

        setores = {}
        for setor in ('palmilha', 'sola', 'pintura'):
            usa_count = sum(1 for l in linhas if l['capacidades_apoio'].get(setor) is not None)
            setores[setor] = usa_count > len(linhas) / 2

        perfis[ref] = {
            'tipo_montagem': tipo_montagem,
            'pools_min_por_par': pools_min_por_par,
            'setores': setores,
        }
    return perfis


def _mix_semana(itens):
    mix = defaultdict(int)
    for item in itens:
        mix[item['ref']] += item['pares']
    return mix


def _pct(consumo, capacidade):
    return round(consumo / capacidade * 100, 1) if capacidade else 0.0


def calcular(cap, prog_detalhe):
    perfis = _montar_perfis_referencia(cap)

    cap_pools_dia = {pool: dados['min_dia_total']
                      for pool, dados in cap['pool_maquinas_injetados'].items()}
    cap_atelier = cap['teto_atelier_semanal']
    setores_apoio = cap['setores_apoio']
    cap_palmilha_sem = setores_apoio['filial_palmilha']['capacidade_dia'] * DIAS_UTEIS_SEMANA
    cap_sola_sem = setores_apoio['filial_sola']['capacidade_dia'] * DIAS_UTEIS_SEMANA
    cap_pintura_sem = round(
        setores_apoio['capacidade_dia_observada']['pintura'] * DIAS_UTEIS_SEMANA, 1)

    semanas_out = {}
    for semana, itens in prog_detalhe['semanas'].items():
        mix = _mix_semana(itens)
        total_pares = sum(mix.values())

        acum_conv = acum_mont = 0
        acum_pool_min = defaultdict(float)
        acum_setor = defaultdict(int)
        nao_mapeadas = []

        for ref, pares in mix.items():
            perfil = perfis.get(ref)
            if perfil is None:
                nao_mapeadas.append({'referencia': ref, 'pares': pares})
                continue

            if perfil['tipo_montagem'] == 'MONTADO':
                acum_mont += pares
            else:
                acum_conv += pares

            for pool, tempo_min in perfil['pools_min_por_par'].items():
                acum_pool_min[pool] += pares * tempo_min

            for setor, usa in perfil['setores'].items():
                if usa:
                    acum_setor[setor] += pares

        atelier = {
            'convencional': {
                'consumo': acum_conv,
                'capacidade': cap_atelier['convencional_pares_semana'],
                'pct': _pct(acum_conv, cap_atelier['convencional_pares_semana']),
            },
            'montado': {
                'consumo': acum_mont,
                'capacidade': cap_atelier['montado_pares_semana'],
                'pct': _pct(acum_mont, cap_atelier['montado_pares_semana']),
            },
        }

        pools_out = {}
        for pool, cap_dia in cap_pools_dia.items():
            consumo_min_dia = round(acum_pool_min.get(pool, 0.0) / DIAS_UTEIS_SEMANA, 1)
            pools_out[pool] = {
                'consumo_min_dia': consumo_min_dia,
                'capacidade_min_dia': cap_dia,
                'pct': _pct(consumo_min_dia, cap_dia),
            }

        setores_out = {
            'filial_palmilha': {
                'consumo': acum_setor.get('palmilha', 0),
                'capacidade': cap_palmilha_sem,
                'pct': _pct(acum_setor.get('palmilha', 0), cap_palmilha_sem),
            },
            'filial_sola': {
                'consumo': acum_setor.get('sola', 0),
                'capacidade': cap_sola_sem,
                'pct': _pct(acum_setor.get('sola', 0), cap_sola_sem),
            },
            'pintura': {
                'consumo': acum_setor.get('pintura', 0),
                'capacidade': cap_pintura_sem,
                'pct': _pct(acum_setor.get('pintura', 0), cap_pintura_sem),
            },
        }

        indicadores = {
            'atelier.convencional': atelier['convencional']['pct'],
            'atelier.montado': atelier['montado']['pct'],
            'setores_apoio.filial_palmilha': setores_out['filial_palmilha']['pct'],
            'setores_apoio.filial_sola': setores_out['filial_sola']['pct'],
            'setores_apoio.pintura': setores_out['pintura']['pct'],
        }
        for pool, dados in pools_out.items():
            indicadores['pools_injetados.' + pool] = dados['pct']

        gargalo, eficiencia_pct = max(indicadores.items(), key=lambda kv: kv[1])

        semanas_out[semana] = {
            'total_pares': total_pares,
            'atelier': atelier,
            'pools_injetados': pools_out,
            'setores_apoio': setores_out,
            'eficiencia_pct': eficiencia_pct,
            'gargalo': gargalo,
            'referencias_nao_mapeadas': nao_mapeadas,
        }

    return semanas_out


def _mes_ref(semana):
    """Mês 'dono' de uma semana = mês da sexta-feira (último dia útil),
    espelhando mesRef() do frontend."""
    ano, mes, dia = (int(p) for p in semana.split('-'))
    sexta = date(ano, mes, dia) + timedelta(days=4)
    return f'{sexta.year:04d}-{sexta.month:02d}'


def _dias_uteis_mes(ano, mes):
    _, ultimo_dia = calendar.monthrange(ano, mes)
    return sum(1 for dia in range(1, ultimo_dia + 1) if date(ano, mes, dia).weekday() < 5)


def agregar_meses(semanas_out, cap):
    """Agrega os indicadores semanais por mês (seção 3 da especificação de
    drill-down): consumo somado das semanas do mês contra a capacidade
    diária de cada teto multiplicada pelos dias úteis reais do mês.
    """
    cap_atelier = cap['teto_atelier_semanal']
    cap_pools_dia = {pool: dados['min_dia_total']
                      for pool, dados in cap['pool_maquinas_injetados'].items()}
    setores_apoio = cap['setores_apoio']

    cap_dia = {
        'atelier.convencional': cap_atelier['convencional_pares_semana'] / DIAS_UTEIS_SEMANA,
        'atelier.montado': cap_atelier['montado_pares_semana'] / DIAS_UTEIS_SEMANA,
        'setores_apoio.filial_palmilha': setores_apoio['filial_palmilha']['capacidade_dia'],
        'setores_apoio.filial_sola': setores_apoio['filial_sola']['capacidade_dia'],
        'setores_apoio.pintura': setores_apoio['capacidade_dia_observada']['pintura'],
    }

    acumulado = {}
    for semana, dados in semanas_out.items():
        mes = _mes_ref(semana)
        ac = acumulado.setdefault(mes, {
            'total_pares': 0,
            'atelier.convencional': 0,
            'atelier.montado': 0,
            'setores_apoio.filial_palmilha': 0,
            'setores_apoio.filial_sola': 0,
            'setores_apoio.pintura': 0,
            'pools_min': defaultdict(float),
        })
        ac['total_pares'] += dados['total_pares']
        ac['atelier.convencional'] += dados['atelier']['convencional']['consumo']
        ac['atelier.montado'] += dados['atelier']['montado']['consumo']
        ac['setores_apoio.filial_palmilha'] += dados['setores_apoio']['filial_palmilha']['consumo']
        ac['setores_apoio.filial_sola'] += dados['setores_apoio']['filial_sola']['consumo']
        ac['setores_apoio.pintura'] += dados['setores_apoio']['pintura']['consumo']
        for pool, pdados in dados['pools_injetados'].items():
            ac['pools_min'][pool] += pdados['consumo_min_dia'] * DIAS_UTEIS_SEMANA

    meses_out = {}
    for mes, ac in acumulado.items():
        ano, mes_num = (int(p) for p in mes.split('-'))
        dias_uteis = _dias_uteis_mes(ano, mes_num)

        atelier = {}
        for chave, campo in (('convencional', 'atelier.convencional'), ('montado', 'atelier.montado')):
            consumo = ac[campo]
            capacidade = round(cap_dia[campo] * dias_uteis, 1)
            atelier[chave] = {'consumo': consumo, 'capacidade': capacidade, 'pct': _pct(consumo, capacidade)}

        pools_out = {}
        for pool, cap_min_dia in cap_pools_dia.items():
            consumo_min_dia = round(ac['pools_min'].get(pool, 0.0) / dias_uteis, 1) if dias_uteis else 0.0
            pools_out[pool] = {
                'consumo_min_dia': consumo_min_dia,
                'capacidade_min_dia': cap_min_dia,
                'pct': _pct(consumo_min_dia, cap_min_dia),
            }

        setores_out = {}
        for setor, campo in (('filial_palmilha', 'setores_apoio.filial_palmilha'),
                              ('filial_sola', 'setores_apoio.filial_sola'),
                              ('pintura', 'setores_apoio.pintura')):
            consumo = ac[campo]
            capacidade = round(cap_dia[campo] * dias_uteis, 1)
            setores_out[setor] = {'consumo': consumo, 'capacidade': capacidade, 'pct': _pct(consumo, capacidade)}

        indicadores = {
            'atelier.convencional': atelier['convencional']['pct'],
            'atelier.montado': atelier['montado']['pct'],
            'setores_apoio.filial_palmilha': setores_out['filial_palmilha']['pct'],
            'setores_apoio.filial_sola': setores_out['filial_sola']['pct'],
            'setores_apoio.pintura': setores_out['pintura']['pct'],
        }
        for pool, dados in pools_out.items():
            indicadores['pools_injetados.' + pool] = dados['pct']

        gargalo, eficiencia_pct = max(indicadores.items(), key=lambda kv: kv[1])

        meses_out[mes] = {
            'total_pares': ac['total_pares'],
            'dias_uteis': dias_uteis,
            'atelier': atelier,
            'pools_injetados': pools_out,
            'setores_apoio': setores_out,
            'eficiencia_pct': eficiencia_pct,
            'gargalo': gargalo,
        }

    return meses_out


def gerar(saida='dados_ocupacao_semanal.json'):
    cap = _carregar_json('dados_capacidade.json')
    prog_detalhe = _carregar_json('dados_programacao_detalhe.json')

    semanas = calcular(cap, prog_detalhe)
    out = {
        'gerado_em': datetime.now().strftime('%d/%m/%Y'),
        'semanas': semanas,
        'meses': agregar_meses(semanas, cap),
    }

    caminho = os.path.join(FRONTEND_DIR, saida)
    with open(caminho, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"    {caminho} gerado com {len(out['semanas'])} semanas e {len(out['meses'])} meses")
    return out


if __name__ == '__main__':
    gerar()
