"""Conexão com o MySQL interno (rede local) usado como fonte de dados.

Lê as credenciais de variáveis de ambiente (ver .env.example). Use
`consultar(sql, params)` para rodar uma query e receber uma lista de dicts
(uma entrada por linha, chaves = nomes das colunas).
"""
import os

import pymysql
import pymysql.cursors


def conectar():
    return pymysql.connect(
        host=os.environ.get('MYSQL_HOST'),
        port=int(os.environ.get('MYSQL_PORT', '3306')),
        user=os.environ.get('MYSQL_USER'),
        password=os.environ.get('MYSQL_PASSWORD'),
        database=os.environ.get('MYSQL_DB') or None,
        cursorclass=pymysql.cursors.DictCursor,
        charset='utf8mb4',
    )


def consultar(sql, params=None):
    conn = conectar()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def achar_coluna_conta_contabil(tabela='mould.v_entradapedidos_extended'):
    """Localiza a coluna de conta contábil na view (tolera variações de nome).
    Retorna o nome exato da coluna, ou None se não encontrada."""
    candidatos = []
    for row in consultar(f"SHOW COLUMNS FROM {tabela}"):
        nome = row.get('Field', '')
        baixo = nome.lower()
        if 'contab' in baixo or (('conta' in baixo) and ('_' in baixo or baixo == 'conta')):
            candidatos.append(nome)
    # Preferir nomes que contenham 'contab'
    for c in candidatos:
        if 'contab' in c.lower():
            return c
    return candidatos[0] if candidatos else None


def achar_coluna_valor_liquido(tabela='mould.v_entradapedidos_extended'):
    """Localiza a coluna "valor líquido" na view, tolerando o nome mojibake
    (ex.: `valorl<U+FFFD>quido`) observado no MySQL interno — basta o nome
    conter "valor" e "quido" (case-insensitive) para casar com qualquer
    variação do acento de "líquido"."""
    for row in consultar(f"SHOW COLUMNS FROM {tabela}"):
        nome = row.get('Field', '')
        baixo = nome.lower()
        if 'valor' in baixo and 'quido' in baixo:
            return nome
    return None


def achar_coluna_valor_total(tabela='mould.v_entradapedidos_extended'):
    """Localiza a coluna "valor total" (bruto: com IPI/frete) na view — usada
    no faturamento para bater com os valores contábeis (faturamento bruto).
    Casa com nomes contendo 'valor' e 'total' (case-insensitive). Retorna o
    nome exato ou None (nesse caso o faturamento cai para o valor líquido)."""
    for row in consultar(f"SHOW COLUMNS FROM {tabela}"):
        nome = row.get('Field', '')
        baixo = nome.lower()
        if 'valor' in baixo and 'total' in baixo:
            return nome
    return None
