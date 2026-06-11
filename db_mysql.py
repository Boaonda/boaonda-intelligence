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
