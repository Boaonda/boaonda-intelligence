#!/usr/bin/env python3
"""Gera frontend/dados_fotos.json buscando imagens de produto no
Inside Boaonda (inside.boaonda.com.br) via WordPress REST API.

Convenção de nome de arquivo no Inside:
    {REF}_{LINHA_CODE}_{COR_CODE}            → ângulo principal (sem sufixo)
    {REF}_{LINHA_CODE}_{COR_CODE}_lateral
    {REF}_{LINHA_CODE}_{COR_CODE}_topo
    {REF}_{LINHA_CODE}_{COR_CODE}_solado

Exemplos reais:
    1317_185_001          → principal
    1317_185_001_topo
    1317_185_001_solado
    1317_185_002_lateral

Onde:
    REF        = número da referência (ex.: 1317 de "1317 NELLIE")
    LINHA_CODE = número entre parênteses no nome da linha (ex.: 185 de "SANDALIA FEM TR (185)")
    COR_CODE   = código da combinação (ex.: 001 de "001 (PRETO/PRETO)")

Uso:
    python gerar_dados_fotos.py [--usuario U --senha S]

Variáveis de ambiente opcionais:
    INSIDE_USER   INSIDE_PASS
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Instale o pacote requests:  pip install requests")
    sys.exit(1)

# ─── Configuração ──────────────────────────────────────────────────────────────
INSIDE_BASE  = "https://inside.boaonda.com.br"
WP_API_MEDIA = f"{INSIDE_BASE}/wp-json/wp/v2/media"

# Ângulo principal = arquivo sem sufixo de ângulo (ex.: "1317_185_001")
# Os demais têm sufixo: _lateral  _topo  _solado
ANGULOS = ["principal", "lateral", "topo", "solado"]

DELAY_S = 0.2   # intervalo entre chamadas à API

BASE_DIR     = Path(__file__).parent
ESTOQUE_PATH = BASE_DIR / "frontend" / "dados_estoque.json"
FOTOS_OUT    = BASE_DIR / "frontend" / "dados_fotos.json"

PLACEHOLDER_URL = (
    "https://via.placeholder.com/600x600/26361e/ed6842"
    "?text=Boaonda"
)


# ─── Helpers ───────────────────────────────────────────────────────────────────
def criar_session(usuario=None, senha=None):
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.6,
                  status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "BoaondaIntelligence/1.0",
                       "Accept": "application/json"})
    if usuario and senha:
        s.auth = (usuario, senha)
    return s


def extrair_ref_num(ref_key: str) -> str:
    """'1317 NELLIE' → '1317'"""
    m = re.match(r'^(\d+)', ref_key.strip())
    return m.group(1) if m else ref_key


def extrair_linha_code(linha_descr: str) -> str | None:
    """'SANDALIA FEM TR (185)' → '185'   |   'BOTA FEM PVC (103) FOSCA' → '103'"""
    m = re.search(r'\((\d+)\)', linha_descr.strip())
    return m.group(1) if m else None


def extrair_cor_code(comb_key: str) -> str:
    """'001 (PRETO/PRETO)' → '001'"""
    return comb_key.strip().split()[0]


def extrair_cor_nome(comb_key: str) -> str:
    """'001 (PRETO/PRETO)' → 'PRETO/PRETO'"""
    m = re.match(r'^\w+\s*\((.+)\)', comb_key.strip())
    return m.group(1).strip() if m else comb_key


def buscar_wp(session, query: str) -> list:
    """Busca na API do WordPress e retorna lista de media items."""
    try:
        r = session.get(
            WP_API_MEDIA,
            params={"search": query, "per_page": 50, "media_type": "image"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            print("\n  ⚠ API retornou 401 — use --usuario e --senha.",
                  file=sys.stderr)
    except requests.exceptions.ConnectionError:
        print(f"\n  ⚠ Sem conexão com {INSIDE_BASE}.", file=sys.stderr)
    except Exception as exc:
        print(f"\n  ⚠ Erro: {exc}", file=sys.stderr)
    return []


def slug_de(item: dict) -> str:
    """Retorna o slug do media item para comparação.
    Prioriza o campo 'slug' do WordPress — mais confiável que o filename,
    que pode conter sufixos automáticos como -scaled ou -rotated.
    """
    slug  = item.get("slug", "")
    src   = item.get("source_url", "")
    title = (item.get("title") or {}).get("rendered", "")
    if slug:
        return slug.lower()
    fname = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return (fname or title).lower()


def fotos_por_prefixo(session, prefixo: str) -> dict:
    """
    Retorna {angulo: url} para um prefixo como '1317_185_001'.

    Ângulos esperados:
        principal → arquivo cujo slug == prefixo (sem sufixo de ângulo)
        lateral   → slug == prefixo + '_lateral'
        topo      → slug == prefixo + '_topo'
        solado    → slug == prefixo + '_solado'
    """
    items = buscar_wp(session, prefixo)
    time.sleep(DELAY_S)

    fotos = {}
    pref_low = prefixo.lower()
    for item in items:
        s = slug_de(item)
        src = item.get("source_url", "")
        if not s or not src:
            continue
        if s == pref_low:
            fotos["principal"] = src
        elif s == f"{pref_low}_lateral":
            fotos["lateral"] = src
        elif s == f"{pref_low}_topo":
            fotos["topo"] = src
        elif s == f"{pref_low}_solado":
            fotos["solado"] = src
    return fotos


# ─── Função chamável pelo Flask ───────────────────────────────────────────────
def gerar(usuario=None, senha=None, estoque_path=None, fotos_out=None):
    """Gera dados_fotos.json e retorna dict com estatísticas.
    Chamada pelo app.py via /admin/fotos (sem usar argparse).
    """
    ep = Path(estoque_path) if estoque_path else ESTOQUE_PATH
    fo = Path(fotos_out)    if fotos_out    else FOTOS_OUT

    with open(ep, encoding="utf-8") as f:
        estoque = json.load(f)

    refs    = estoque.get("refs", {})
    session = criar_session(usuario, senha)
    resultado: dict = {}
    stats = {"total": 0, "completas": 0, "parciais": 0, "sem_foto": 0}

    for ref_key, ref_data in refs.items():
        ref_num  = extrair_ref_num(ref_key)
        cors_map: dict[str, dict] = {}

        for linha_descr, ln_data in ref_data.get("linhas", {}).items():
            linha_code = extrair_linha_code(linha_descr)
            if not linha_code:
                continue
            for comb_key in ln_data.get("combinacoes", {}):
                if comb_key in cors_map:
                    continue
                cor_code = extrair_cor_code(comb_key)
                cor_nome = extrair_cor_nome(comb_key)
                prefixo  = f"{ref_num}_{linha_code}_{cor_code}"
                stats["total"] += 1

                fotos = fotos_por_prefixo(session, prefixo)
                n     = len(fotos)
                entry = {a: fotos.get(a) for a in ANGULOS}
                entry["cor_nome"]    = cor_nome
                entry["placeholder"] = PLACEHOLDER_URL
                cors_map[comb_key]   = entry

                if n == len(ANGULOS):
                    stats["completas"] += 1
                elif n > 0:
                    stats["parciais"] += 1
                else:
                    stats["sem_foto"] += 1

        resultado[ref_key] = cors_map

    saida = {
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "inside_base": INSIDE_BASE,
        "refs": resultado,
    }
    with open(fo, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)

    stats["cobertura_pct"] = round(
        (stats["completas"] + stats["parciais"]) / max(stats["total"], 1) * 100
    )
    return stats


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--usuario", default=os.environ.get("INSIDE_USER"))
    parser.add_argument("--senha",   default=os.environ.get("INSIDE_PASS"))
    args = parser.parse_args()

    print(f"Carregando {ESTOQUE_PATH.name}...")
    with open(ESTOQUE_PATH, encoding="utf-8") as f:
        estoque = json.load(f)

    refs = estoque.get("refs", {})
    print(f"  {len(refs)} referências | "
          f"{'Com autenticação' if args.usuario else 'Sem autenticação (público)'}\n")

    session = criar_session(args.usuario, args.senha)
    resultado: dict = {}
    stats = {"total": 0, "completas": 0, "parciais": 0, "sem_foto": 0}

    for ref_key, ref_data in refs.items():
        ref_num = extrair_ref_num(ref_key)
        resultado[ref_key] = {}
        # Mapeia comb_key → {angulo: url} consolidando de todas as linhas
        cors_map: dict[str, dict] = {}

        for linha_descr, ln_data in ref_data.get("linhas", {}).items():
            linha_code = extrair_linha_code(linha_descr)
            if not linha_code:
                continue  # linha sem código numérico — ignora

            for comb_key in ln_data.get("combinacoes", {}):
                if comb_key in cors_map:
                    continue  # já processada por outra linha desta ref
                cor_code = extrair_cor_code(comb_key)
                cor_nome = extrair_cor_nome(comb_key)

                prefixo = f"{ref_num}_{linha_code}_{cor_code}"
                stats["total"] += 1
                print(f"  {ref_num}/{linha_code}/{cor_code} ({cor_nome})... ",
                      end="", flush=True)

                fotos = fotos_por_prefixo(session, prefixo)
                n = len(fotos)

                entry = {a: fotos.get(a) for a in ANGULOS}
                entry["cor_nome"]    = cor_nome
                entry["placeholder"] = PLACEHOLDER_URL
                cors_map[comb_key] = entry

                if n == len(ANGULOS):
                    stats["completas"] += 1
                    print(f"✓ {n}/{len(ANGULOS)}")
                elif n > 0:
                    stats["parciais"] += 1
                    ok = ", ".join(a for a in ANGULOS if fotos.get(a))
                    print(f"~ {n}/{len(ANGULOS)} ({ok})")
                else:
                    stats["sem_foto"] += 1
                    print("✗ 0")

        resultado[ref_key] = cors_map

    saida = {
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "inside_base": INSIDE_BASE,
        "refs": resultado,
    }

    with open(FOTOS_OUT, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*54}")
    print(f"Total processado           : {stats['total']}")
    print(f"Com todos os ângulos       : {stats['completas']}")
    print(f"Parcialmente encontradas   : {stats['parciais']}")
    print(f"Sem nenhuma foto           : {stats['sem_foto']}")
    pct = round((stats['completas'] + stats['parciais']) /
                max(stats['total'], 1) * 100)
    print(f"Cobertura                  : {pct}%")
    print(f"\n✓ Salvo em: {FOTOS_OUT}")


if __name__ == "__main__":
    main()
