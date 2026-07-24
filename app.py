from pathlib import Path
from datetime import datetime
import html
import io
import json
import os
import re
import shutil
import threading
import traceback

from flask import (Flask, request, jsonify, send_from_directory, send_file,
                    session, redirect, url_for, render_template_string, render_template)
from werkzeug.security import generate_password_hash, check_password_hash

import processador

BASE_DIR     = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / 'frontend'

# DATA_DIR guarda os JSONs gerados e os uploads — em produção (Fly.io) aponta
# para um volume persistente, sobrevivendo a redeploys. Em dev local, usa a
# própria pasta frontend/ (comportamento de antes).
DATA_DIR = Path(os.environ.get('DATA_DIR', str(FRONTEND_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = DATA_DIR / 'uploads'
UPLOADS_DIR.mkdir(exist_ok=True)

DATA_FILES = (
    'dados_estoque.json', 'dados_portal.json', 'dados_programacao.json',
    'dados_programacao_detalhe.json',
    'dados_refs_tabela.json', 'dados_vendas.json', 'dados_vendas_eva.json',
    'dados_vendas_clientes.json', 'dados_vendas_carteira.json',
    'dados_carteira.json',
    'boaonda_dados_completos.json', 'config_producao.json',
    'dados_capacidade.json', 'dados_ocupacao_semanal.json',
    'dados_faturamento.json', 'dados_fotos.json', 'dados_home.json',
    'dados_metas.json',
)

# Arquivos JSON servidos publicamente (sem autenticação) para o catálogo público.
DATA_FILES_PUBLICOS = {'dados_estoque.json', 'dados_fotos.json', 'dados_home.json'}

# Páginas do cluster de Vendas que usam a sidebar de navegação compartilhada
# (frontend/_sidebar_vendas.html, incluída via Jinja {% include %} — fonte
# única, sem duplicar HTML/CSS/JS em cada arquivo). Servidas via
# render_template() em vez de send_from_directory() só para essas; o
# restante do site continua estático puro. Ver serve_file() abaixo.
PAGINAS_COM_SIDEBAR_VENDAS = {
    'boaonda_vendas_catalogo.html',
    'boaonda_carteira_clientes.html',
    'boaonda_carteira_representante.html',
    'boaonda_carteira_uf.html',
    'boaonda_metas_comerciais.html',
}

# Primeira execução com volume vazio: semeia com os JSONs versionados no repo
if DATA_DIR != FRONTEND_DIR:
    for _fname in DATA_FILES:
        _dst = DATA_DIR / _fname
        _src = FRONTEND_DIR / _fname
        if not _dst.exists() and _src.exists():
            shutil.copy(_src, _dst)

app = Flask(__name__, static_folder=None, template_folder=str(FRONTEND_DIR))
app.secret_key = os.environ.get('SECRET_KEY', 'dev-local-key-change-in-prod')
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024  # 250MB — 3YS.csv pode ter ~130MB

# ── Usuários (armazenados em DATA_DIR/usuarios.json, senha sempre hasheada) ────
USUARIOS_FILE = DATA_DIR / 'usuarios.json'

# Rotas restritas a usuários com role 'admin' — gestão de usuários, atualização
# de dados/fotos/home do catálogo e configurações. Dashboards e o catálogo
# público continuam abertos a qualquer usuário logado (ou sem login, no caso
# do catálogo). Checado por prefixo de path em require_login().
ADMIN_ONLY_PREFIXES = ('/admin/', '/upload', '/config')
ADMIN_ONLY_EXATOS = {'/api/capacidade/importar', '/api/metas/importar',
                     '/api/metas/exportar'}


def _ler_usuarios():
    try:
        return json.loads(USUARIOS_FILE.read_text(encoding='utf-8')).get('usuarios', [])
    except Exception:
        return []


def _salvar_usuarios(lista):
    USUARIOS_FILE.write_text(
        json.dumps({'usuarios': lista}, ensure_ascii=False, indent=2), encoding='utf-8')


def _achar_usuario(username):
    if not username:
        return None
    return next((u for u in _ler_usuarios() if u['username'] == username), None)


def _seed_usuarios_iniciais():
    """Primeira execução (usuarios.json ainda não existe): cria o admin a
    partir das variáveis de ambiente legadas (AUTH_USERNAME/AUTH_PASSWORD),
    preservando o acesso de quem já usava o login único antes desta tela
    de gestão de usuários existir."""
    if USUARIOS_FILE.exists():
        return
    username = os.environ.get('AUTH_USERNAME', 'admin')
    password = os.environ.get('AUTH_PASSWORD', 'analytics2024')
    _salvar_usuarios([{
        'username': username,
        'senha_hash': generate_password_hash(password),
        'role': 'admin',
        'criado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
    }])


_seed_usuarios_iniciais()

# ── Módulos (dashboards) — controle de acesso granular por usuário comum ──────
# Cada módulo mapeia os arquivos HTML do dashboard, os JSONs de dados que ele
# usa (só os que NÃO são públicos — dados_estoque.json/dados_fotos.json/
# dados_home.json continuam públicos para o catálogo, então não entram aqui)
# e os contextos de IA equivalentes (ver IA_CONTEXTO_ARQUIVOS/IA_RESUMIDORES
# mais abaixo, na seção de Inteligência).
MODULOS = {
    'vendas': {
        'label': 'Vendas Calçados',
        'htmls': ['boaonda_vendas_catalogo.html', 'boaonda_carteira_clientes.html', 'boaonda_carteira_representante.html', 'boaonda_carteira_uf.html', 'boaonda_metas_comerciais.html'],
        'jsons': ['dados_vendas.json', 'dados_vendas_clientes.json', 'dados_vendas_carteira.json', 'dados_metas.json'],
        'ia': ['vendas'],
    },
    'vendas_eva': {
        'label': 'Vendas Composto EVA',
        'htmls': ['boaonda_vendas_eva.html'],
        'jsons': ['dados_vendas_eva.json'],
        'ia': ['vendas_eva'],
    },
    'carteira': {
        'label': 'Pedido em Carteira',
        'htmls': ['boaonda_carteira.html'],
        'jsons': ['dados_carteira.json'],
        'ia': ['carteira'],
    },
    'programacao': {
        'label': 'Programação',
        'htmls': ['boaonda_programacao_v3.html'],
        'jsons': ['dados_programacao.json', 'dados_programacao_detalhe.json',
                  'dados_ocupacao_semanal.json', 'dados_refs_tabela.json'],
        'ia': ['programacao', 'ocupacao', 'refs'],
    },
    'capacidade': {
        'label': 'Capacidade Fabril',
        'htmls': ['boaonda_capacidade_fabril.html'],
        'jsons': ['dados_capacidade.json'],
        'ia': ['capacidade'],
    },
    'estoque': {
        'label': 'Estoque',
        'htmls': ['boaonda_estoque.html'],
        # dados_estoque.json é público (alimenta o catálogo sem login) — não
        # dá para restringir o arquivo em si, só a tela do dashboard interno.
        'jsons': [],
        'ia': ['estoque'],
    },
    'faturamento': {
        'label': 'Faturamento',
        'htmls': ['boaonda_faturamento.html'],
        'jsons': ['dados_faturamento.json'],
        'ia': ['faturamento'],
    },
    'catalogo_leads': {
        'label': 'Pedidos de Pronta Entrega',
        'htmls': ['boaonda_catalogo_leads.html'],
        # Sem JSON estático — os dados vêm ao vivo do Supabase via
        # /api/catalogo/dados e /api/catalogo/resumo (ver rotas abaixo), não
        # de um arquivo pré-gerado como os demais módulos.
        'jsons': [],
        'ia': [],
    },
}
MODULOS_KEYS = list(MODULOS.keys())

# Reverse lookups, construídos uma vez a partir de MODULOS acima.
_ARQUIVO_PARA_MODULO = {}
_IA_CONTEXTO_PARA_MODULO = {}
for _mk, _mv in MODULOS.items():
    for _f in _mv['htmls'] + _mv['jsons']:
        _ARQUIVO_PARA_MODULO[_f] = _mk
    for _ctx in _mv['ia']:
        _IA_CONTEXTO_PARA_MODULO[_ctx] = _mk


def _modulos_do_usuario(usuario):
    """Conjunto de módulos que o usuário pode acessar. Admin sempre tem
    acesso total; usuário comum sem o campo 'modulos' definido (criado antes
    desta funcionalidade existir) também tem acesso total, por
    compatibilidade — a restrição é opt-out, não opt-in."""
    if not usuario:
        return set()
    if usuario.get('role') == 'admin':
        return set(MODULOS_KEYS)
    modulos = usuario.get('modulos')
    if modulos is None:
        return set(MODULOS_KEYS)
    return set(modulos) & set(MODULOS_KEYS)


def _exige_modulo(modulo_key):
    """Retorna uma resposta 403 se o usuário logado não tem acesso ao módulo
    indicado, ou None se pode prosseguir. Usado em rotas de API (export etc)
    que não passam pelo gate de arquivo estático do serve_file()."""
    usuario = _achar_usuario(session.get('username'))
    if modulo_key not in _modulos_do_usuario(usuario):
        return jsonify({'erro': f'Você não tem acesso ao módulo '
                                 f'"{MODULOS[modulo_key]["label"]}". '
                                 f'Fale com um administrador.'}), 403
    return None


# ─────────────────────────────────────────────
#  AUTENTICAÇÃO
# ─────────────────────────────────────────────
_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f8f5f1;color:#26361e;font-family:'Montserrat',system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#fff;border:1px solid #e2ddd8;border-radius:14px;
     padding:40px 36px;width:100%;max-width:360px;box-shadow:0 8px 24px rgba(38,54,30,.08)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:28px;justify-content:center}
.logo-text{font-size:1.25rem;font-weight:800;letter-spacing:2px;color:#ed6842}
.logo-accent{color:#26361e;font-weight:300;font-size:.95rem;letter-spacing:1px;margin-left:6px}
h2{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
   color:#9b9895;text-align:center;margin-bottom:24px}
.field{margin-bottom:16px}
label{display:block;font-size:.7rem;font-weight:700;text-transform:uppercase;
      letter-spacing:.06em;color:#71706f;margin-bottom:6px}
input{width:100%;background:#f3f0eb;border:1px solid #e2ddd8;border-radius:8px;
      padding:10px 14px;color:#26361e;font-size:.9rem;outline:none;transition:.2s}
input:focus{border-color:#ed6842}
.btn{width:100%;background:#ed6842;color:#fff;border:none;border-radius:8px;
     padding:12px;font-size:.9rem;font-weight:700;cursor:pointer;margin-top:8px;
     letter-spacing:.03em;transition:.2s}
.btn:hover{background:#dd7051}
.err{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:8px;
     padding:10px 14px;font-size:.8rem;color:#c0392b;margin-bottom:16px;text-align:center}
.pw-wrap{position:relative}
.pw-wrap input{padding-right:42px}
.eye{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;
     border:none;color:#9b9895;cursor:pointer;font-size:1rem;padding:2px;line-height:1}
.eye:hover{color:#ed6842}
</style>
</head>
<body>
<div class="box">
  <div class="logo">
    <span class="logo-text">BOAONDA</span><span class="logo-accent">Intelligence</span>
  </div>
  <h2>Acesso Restrito</h2>
  {% if error %}
  <div class="err">{{ error }}</div>
  {% endif %}
  <form method="post">
    <div class="field">
      <label>Usuário</label>
      <input type="text" name="username" autocomplete="username" autofocus
             autocorrect="off" autocapitalize="none" spellcheck="false" required/>
    </div>
    <div class="field">
      <label>Senha</label>
      <div class="pw-wrap">
        <input type="password" id="pw" name="password" autocomplete="current-password" required/>
        <button type="button" class="eye" onclick="var p=document.getElementById('pw');p.type=p.type==='password'?'text':'password';this.textContent=p.type==='password'?'👁':'🙈'" title="Mostrar/ocultar senha">👁</button>
      </div>
    </div>
    <button class="btn" type="submit">Entrar</button>
  </form>
</div>
</body>
</html>'''


_CATALOGO_ENTRAR_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Catálogo BOAONDA — Acesso</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--border:#e2ddd8;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Montserrat',system-ui,sans-serif;background:var(--bg);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:40px 36px;
      max-width:380px;width:100%;box-shadow:0 8px 32px rgba(38,54,30,.08)}
.mark{font-size:20px;font-weight:800;letter-spacing:2px;color:var(--coral);margin-bottom:6px}
h1{font-size:18px;font-weight:700;color:var(--verde-dark);margin-bottom:8px}
p.sub{font-size:12.5px;color:var(--txt-m);margin-bottom:22px;line-height:1.5}
label{font-size:11px;font-weight:600;color:var(--verde-dark);display:block;margin-bottom:5px}
input{width:100%;padding:11px 12px;border:1px solid var(--border);border-radius:8px;
      font-family:inherit;font-size:14px;outline:none;transition:.15s}
input:focus{border-color:var(--coral)}
.btn{width:100%;margin-top:16px;padding:12px;border:none;border-radius:8px;background:var(--coral);
     color:#fff;font-family:inherit;font-weight:700;font-size:13px;cursor:pointer;transition:.15s}
.btn:hover{background:#dd5a34}
.erro{background:rgba(221,112,81,.1);color:#b8462a;font-size:12px;padding:9px 12px;
      border-radius:7px;margin-bottom:16px}
.tabs{display:flex;gap:6px;margin-bottom:22px;background:var(--bg);border-radius:9px;padding:4px}
.tab-btn{flex:1;text-align:center;padding:9px 6px;font-size:12px;font-weight:700;color:var(--txt-m);
  background:none;border:none;border-radius:7px;cursor:pointer;font-family:inherit;transition:.15s}
.tab-btn.active{background:#fff;color:var(--verde-dark);box-shadow:0 1px 4px rgba(0,0,0,.08)}
.tab-pane{display:none}
.tab-pane.active{display:block}
</style>
</head>
<body>
<div class="card">
  <div class="mark">BOAONDA</div>
  <h1>Acesse o catálogo</h1>
  <div class="tabs">
    <button type="button" class="tab-btn" id="tabbtn-cliente" onclick="mostrarTab('cliente')">Sou cliente</button>
    <button type="button" class="tab-btn" id="tabbtn-rep" onclick="mostrarTab('representante')">Sou representante</button>
  </div>

  <div class="tab-pane" id="tab-cliente">
    <p class="sub">Informe o CNPJ da sua empresa. Se for a primeira vez, pediremos mais alguns dados rapidinho.</p>
    {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
    <form method="POST" action="/catalogo/entrar">
      <label>CNPJ</label>
      <input type="text" name="cnpj" id="cnpj" placeholder="00.000.000/0000-00" maxlength="18" required/>
      <button class="btn" type="submit">Entrar</button>
    </form>
  </div>

  <div class="tab-pane" id="tab-representante">
    <p class="sub">Acesso restrito a representantes cadastrados pela BOAONDA.</p>
    {% if erro_rep %}<div class="erro">{{ erro_rep }}</div>{% endif %}
    <form method="POST" action="/catalogo/representante/entrar">
      <label>E-mail</label>
      <input type="email" name="email" required/>
      <label style="margin-top:12px">Senha</label>
      <input type="password" name="senha" required/>
      <button class="btn" type="submit">Entrar</button>
    </form>
  </div>
</div>
<script>
function mostrarTab(tab){
  document.querySelectorAll('.tab-pane').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
  document.getElementById('tab-'+tab).classList.add('active');
  document.getElementById(tab==='cliente'?'tabbtn-cliente':'tabbtn-rep').classList.add('active');
}
mostrarTab('{{ tab_ativa }}');
document.getElementById('cnpj')?.addEventListener('input', function(){
  let v = this.value.replace(/\\D/g,'').substring(0,14);
  if      (v.length > 12) v = v.replace(/^(\\d{2})(\\d{3})(\\d{3})(\\d{4})(\\d{0,2})$/,'$1.$2.$3/$4-$5');
  else if (v.length >  8) v = v.replace(/^(\\d{2})(\\d{3})(\\d{3})(\\d{0,4})$/,'$1.$2.$3/$4');
  else if (v.length >  5) v = v.replace(/^(\\d{2})(\\d{0,3})$/,'$1.$2.$3');
  else if (v.length >  2) v = v.replace(/^(\\d{2})(\\d{0,3})$/,'$1.$2');
  this.value = v;
});
</script>
</body>
</html>'''


_CATALOGO_CADASTRO_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Catálogo BOAONDA — Cadastro</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--border:#e2ddd8;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Montserrat',system-ui,sans-serif;background:var(--bg);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:36px;
      max-width:440px;width:100%;box-shadow:0 8px 32px rgba(38,54,30,.08)}
.mark{font-size:20px;font-weight:800;letter-spacing:2px;color:var(--coral);margin-bottom:6px}
h1{font-size:18px;font-weight:700;color:var(--verde-dark);margin-bottom:6px}
p.sub{font-size:12.5px;color:var(--txt-m);margin-bottom:20px;line-height:1.5}
.row{display:flex;gap:10px}
.row > div{flex:1}
label{font-size:11px;font-weight:600;color:var(--verde-dark);display:block;margin:12px 0 5px}
input,select{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;
      font-family:inherit;font-size:13.5px;outline:none;transition:.15s;background:#fff}
input:focus,select:focus{border-color:var(--coral)}
.termos{display:flex;align-items:flex-start;gap:8px;margin-top:18px}
.termos input{width:auto;margin-top:2px}
.termos label{font-size:11.5px;font-weight:400;color:var(--txt-m);margin:0;line-height:1.4}
.btn{width:100%;margin-top:20px;padding:12px;border:none;border-radius:8px;background:var(--coral);
     color:#fff;font-family:inherit;font-weight:700;font-size:13px;cursor:pointer}
.btn:hover{background:#dd5a34}
.erro{background:rgba(221,112,81,.1);color:#b8462a;font-size:12px;padding:9px 12px;
      border-radius:7px;margin-bottom:14px}
</style>
</head>
<body>
<div class="card">
  <div class="mark">BOAONDA</div>
  <h1>Primeiro acesso — complete seu cadastro</h1>
  <p class="sub">CNPJ {{ cnpj }} ainda não cadastrado. Preencha os dados abaixo para liberar o catálogo.</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="POST" action="/catalogo/cadastrar">
    <input type="hidden" name="cnpj" value="{{ cnpj }}"/>
    <label>Nome completo *</label>
    <input type="text" name="nome" required autofocus/>
    <label>Empresa</label>
    <input type="text" name="empresa"/>
    <div class="row">
      <div>
        <label>Telefone *</label>
        <input type="text" name="telefone" required/>
      </div>
      <div>
        <label>E-mail *</label>
        <input type="email" name="email" required/>
      </div>
    </div>
    <div class="row">
      <div>
        <label>Cidade</label>
        <input type="text" name="cidade"/>
      </div>
      <div>
        <label>UF</label>
        <input type="text" name="uf" maxlength="2" style="text-transform:uppercase"/>
      </div>
    </div>
    <label>Representante</label>
    <input type="text" name="representante" placeholder="Nome do representante, ou &quot;sem representante&quot;"/>
    <div class="termos">
      <input type="checkbox" name="aceite_termos" id="aceite" required/>
      <label for="aceite">Concordo com o uso dos meus dados para contato comercial da BOAONDA, conforme a política de privacidade.</label>
    </div>
    <button class="btn" type="submit">Concluir cadastro e entrar</button>
  </form>
</div>
</body>
</html>'''


@app.before_request
def require_login():
    """Bloqueia todas as rotas exceto /login, /logout e o catálogo público.
    Também bloqueia rotas admin-only (ADMIN_ONLY_PREFIXES/EXATOS) para
    usuários com role != 'admin'."""
    public_endpoints = {'login', 'logout', 'catalogo', 'catalogo_entrar', 'catalogo_cadastrar',
                        'catalogo_sair', 'api_catalogo_pedido', 'api_catalogo_quem_sou_eu',
                        'api_catalogo_meu_historico', 'foto_proxy', 'promo_imagem', 'promo_imagem_idx',
                        'catalogo_representante_entrar', 'catalogo_representante_sair',
                        'catalogo_representante_painel', 'catalogo_representante_atuar',
                        'catalogo_representante_cadastrar_cliente'}
    if request.endpoint in public_endpoints:
        return None
    # JSONs necessários para o catálogo público não exigem autenticação
    if request.path.lstrip('/') in DATA_FILES_PUBLICOS:
        return None
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # Revalida o usuário a cada request (não confia em role cacheada na sessão)
    # — se o usuário foi removido ou teve a role alterada, o efeito é imediato.
    usuario = _achar_usuario(session.get('username'))
    if not usuario:
        session.clear()
        return redirect(url_for('login'))
    session['role'] = usuario.get('role', 'comum')
    path = request.path
    eh_admin_only = path.startswith(ADMIN_ONLY_PREFIXES) or path in ADMIN_ONLY_EXATOS
    if eh_admin_only and usuario.get('role') != 'admin':
        return jsonify({'erro': 'Acesso restrito a administradores.'}), 403


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        usuario = _achar_usuario(username)
        if usuario and check_password_hash(usuario['senha_hash'], password):
            session['logged_in'] = True
            session['username']  = username
            session['role']      = usuario.get('role', 'comum')
            return redirect(url_for('index'))
        error = 'Usuário ou senha incorretos.'
    return render_template_string(_LOGIN_HTML, error=error)


@app.route('/api/whoami')
def api_whoami():
    """Usado pelo portal (index.html) para mostrar/ocultar links admin-only
    (Atualizar dados, Gerenciar usuários) e travar os módulos/dashboards que
    o usuário não tem acesso, via JS, sem duplicar a lógica de permissão no
    HTML estático."""
    usuario = _achar_usuario(session.get('username'))
    return jsonify({
        'username': session.get('username'),
        'role': session.get('role', 'comum'),
        'modulos': sorted(_modulos_do_usuario(usuario)),
    })


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ─────────────────────────────────────────────
#  ROTAS
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


def _conectar_catalogo_db():
    """Conexão com o Supabase, reaproveitada pelas rotas do catálogo.
    Mesma lógica da rota /admin/db-tunnel-status."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ.get('DB_NAME', 'postgres'),
        user=os.environ.get('DB_USER'),
        password=os.environ.get('DB_PASSWORD'),
        connect_timeout=5,
    )


@app.route('/catalogo')
def catalogo():
    """Catálogo público de produtos. Cliente comum só entra com CNPJ
    confirmado; representante pode navegar livremente assim que loga —
    só é obrigado a escolher/cadastrar um cliente na hora de fechar o
    pedido (checado em /api/catalogo/pedido), não pra só olhar o catálogo."""
    if session.get('catalogo_cadastro_id') or session.get('catalogo_rep_id'):
        return send_from_directory(FRONTEND_DIR, 'catalogo.html')
    return redirect(url_for('catalogo_entrar'))


@app.route('/catalogo/sair')
def catalogo_sair():
    """Encerra o contexto do CNPJ atual. Se a sessão pertence a um
    representante atuando em nome de um cliente, volta pro painel dele
    (sem derrubar o login do representante) — só um cliente puro (sem
    representante por trás) volta pra tela de entrada."""
    session.pop('catalogo_cadastro_id', None)
    session.pop('catalogo_cliente', None)
    if session.get('catalogo_rep_id'):
        return redirect(url_for('catalogo_representante_painel'))
    return redirect(url_for('catalogo_entrar'))


@app.route('/catalogo/entrar', methods=['GET', 'POST'])
def catalogo_entrar():
    """Primeira tela: pede só o CNPJ. Se já cadastrado, libera direto.
    Se não, encaminha para o cadastro completo."""
    erro = None
    if request.method == 'POST':
        cnpj = re.sub(r'\D', '', request.form.get('cnpj', ''))
        if len(cnpj) != 14:
            erro = 'CNPJ inválido — confira os números digitados.'
        else:
            try:
                conexao = _conectar_catalogo_db()
                cursor = conexao.cursor()
                cursor.execute(
                    "SELECT id, nome, empresa, representante FROM catalogo_cadastros WHERE cnpj = %s",
                    (cnpj,)
                )
                row = cursor.fetchone()
                conexao.close()
                if row:
                    session['catalogo_cadastro_id'] = str(row[0])
                    session['catalogo_cliente'] = {
                        'nome': row[1], 'empresa': row[2] or '', 'representante': row[3],
                    }
                    return redirect(url_for('catalogo'))
                # CNPJ não encontrado: mostra o formulário completo, já com o CNPJ preenchido
                return render_template_string(_CATALOGO_CADASTRO_HTML, cnpj=cnpj, erro=None)
            except Exception as ex:
                erro = f'Não foi possível consultar o cadastro agora. Tente novamente. ({ex})'
    return render_template_string(_CATALOGO_ENTRAR_HTML, erro=erro, erro_rep=None, tab_ativa='cliente')


@app.route('/catalogo/cadastrar', methods=['POST'])
def catalogo_cadastrar():
    """Grava um cadastro novo (CNPJ não encontrado na etapa anterior)."""
    nome         = request.form.get('nome', '').strip()
    empresa      = request.form.get('empresa', '').strip() or None
    cnpj         = re.sub(r'\D', '', request.form.get('cnpj', ''))
    telefone     = request.form.get('telefone', '').strip()
    email        = request.form.get('email', '').strip()
    cidade       = request.form.get('cidade', '').strip() or None
    uf           = (request.form.get('uf', '').strip() or None)
    representante = request.form.get('representante', '').strip() or 'sem representante'
    aceite       = request.form.get('aceite_termos') == 'on'

    def _erro(msg):
        return render_template_string(_CATALOGO_CADASTRO_HTML, cnpj=cnpj, erro=msg)

    if not (nome and telefone and email and len(cnpj) == 14):
        return _erro('Preencha nome, telefone, e-mail e um CNPJ válido.')
    if not aceite:
        return _erro('É necessário aceitar os termos para continuar.')

    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            INSERT INTO catalogo_cadastros
                (nome, empresa, cnpj, telefone, email, cidade, uf, representante, aceite_termos)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (nome, empresa, cnpj, telefone, email, cidade, uf, representante, aceite))
        novo_id = cursor.fetchone()[0]
        conexao.commit()
        conexao.close()
        session['catalogo_cadastro_id'] = str(novo_id)
        session['catalogo_cliente'] = {'nome': nome, 'empresa': empresa or '', 'representante': representante}
        return redirect(url_for('catalogo'))
    except Exception as ex:
        # Erro de UNIQUE (cnpj) cai aqui também — psycopg2.IntegrityError é subclasse de Exception
        if 'unique' in str(ex).lower() or 'duplicate' in str(ex).lower():
            return _erro('Esse CNPJ já está cadastrado — volte e informe só o CNPJ para entrar.')
        return _erro(f'Erro ao cadastrar: {ex}')


# ─────────────────────────────────────────────
#  REPRESENTANTE — login próprio, carteira de clientes cadastrados por
#  ele mesmo, e atuação "em nome de" um cliente pra digitar pedido oficial.
#  Cadastro de representante é feito só pelo admin (ver /admin/catalogo-
#  representantes); aqui só existe o login e o uso do dia a dia.
# ─────────────────────────────────────────────
_CATALOGO_REP_PAINEL_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Catálogo BOAONDA — Meus clientes</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--border:#e2ddd8;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Montserrat',system-ui,sans-serif;background:var(--bg);min-height:100vh;padding:32px 20px}
.wrap{max-width:640px;margin:0 auto}
.topo{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}
.mark{font-size:18px;font-weight:800;letter-spacing:2px;color:var(--coral)}
.mark span{color:var(--verde-dark);font-weight:400;font-size:12px;margin-left:8px;letter-spacing:1px}
.sair{font-size:12px;color:var(--txt-m);text-decoration:none;border:1px solid var(--border);border-radius:7px;padding:7px 12px}
.sair:hover{color:#b8462a;border-color:#b8462a}
h1{font-size:16px;font-weight:700;color:var(--verde-dark);margin-bottom:4px}
p.sub{font-size:12.5px;color:var(--txt-m);margin-bottom:18px}
.erro{background:rgba(221,112,81,.1);color:#b8462a;font-size:12px;padding:9px 12px;border-radius:7px;margin-bottom:16px}
.btn{display:inline-block;background:var(--coral);color:#fff;font-weight:700;font-size:12.5px;
     text-decoration:none;padding:10px 18px;border-radius:8px;margin-bottom:20px}
.btn:hover{background:#dd5a34}
.card{background:#fff;border:1px solid var(--border);border-radius:12px;padding:0;overflow:hidden}
.cli{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border)}
.cli:last-child{border-bottom:none}
.cli-info b{font-size:13px;color:var(--verde-dark);display:block}
.cli-info span{font-size:11.5px;color:var(--txt-m)}
.cli-info .pedidos{display:block;margin-top:2px;font-size:11px;color:var(--txt-m)}
.cli-info .pedidos.tem{color:#6c9c37;font-weight:600}
.cli-btn{font-size:12px;font-weight:700;color:var(--coral);text-decoration:none;border:1px solid var(--coral);
         border-radius:7px;padding:7px 14px;white-space:nowrap}
.cli-btn:hover{background:var(--coral);color:#fff}
.vazio{padding:24px 18px;font-size:12.5px;color:var(--txt-m);text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <div class="topo">
    <div class="mark">BOAONDA <span>· Representante</span></div>
    <a class="sair" href="/catalogo/representante/sair">↪ Sair</a>
  </div>
  <h1>Olá, {{ rep_nome }}</h1>
  <p class="sub">Clientes que você cadastrou. Escolha um para digitar o pedido em nome dele.</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <a class="btn" href="/catalogo/representante/cadastrar-cliente">+ Cadastrar novo cliente</a>
  <div class="card">
    {% if clientes %}
      {% for c in clientes %}
      <div class="cli">
        <div class="cli-info">
          <b>{{ c.empresa or c.nome }}</b>
          <span>{{ c.nome }}{% if c.cidade %} · {{ c.cidade }}{% if c.uf %}/{{ c.uf }}{% endif %}{% endif %}</span>
          {% if c.total_pedidos %}
          <span class="pedidos tem">{{ c.total_pedidos }} pedido{{ 's' if c.total_pedidos != 1 else '' }} · último em {{ c.ultimo_pedido }}</span>
          {% else %}
          <span class="pedidos">Nenhum pedido ainda</span>
          {% endif %}
        </div>
        <a class="cli-btn" href="/catalogo/representante/atuar/{{ c.id }}">Digitar pedido</a>
      </div>
      {% endfor %}
    {% else %}
      <div class="vazio">Você ainda não cadastrou nenhum cliente.</div>
    {% endif %}
  </div>
</div>
</body>
</html>'''


_CATALOGO_REP_CADASTRO_CLIENTE_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Catálogo BOAONDA — Novo cliente</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--border:#e2ddd8;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Montserrat',system-ui,sans-serif;background:var(--bg);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:36px;
      max-width:440px;width:100%;box-shadow:0 8px 32px rgba(38,54,30,.08)}
.mark{font-size:20px;font-weight:800;letter-spacing:2px;color:var(--coral);margin-bottom:6px}
h1{font-size:18px;font-weight:700;color:var(--verde-dark);margin-bottom:6px}
p.sub{font-size:12.5px;color:var(--txt-m);margin-bottom:20px;line-height:1.5}
.row{display:flex;gap:10px}
.row > div{flex:1}
label{font-size:11px;font-weight:600;color:var(--verde-dark);display:block;margin:12px 0 5px}
input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;
      font-family:inherit;font-size:13.5px;outline:none;transition:.15s;background:#fff}
input:focus{border-color:var(--coral)}
.termos{display:flex;align-items:flex-start;gap:8px;margin-top:18px}
.termos input{width:auto;margin-top:2px}
.termos label{font-size:11.5px;font-weight:400;color:var(--txt-m);margin:0;line-height:1.4}
.btn{width:100%;margin-top:20px;padding:12px;border:none;border-radius:8px;background:var(--coral);
     color:#fff;font-family:inherit;font-weight:700;font-size:13px;cursor:pointer}
.btn:hover{background:#dd5a34}
.erro{background:rgba(221,112,81,.1);color:#b8462a;font-size:12px;padding:9px 12px;
      border-radius:7px;margin-bottom:14px}
.voltar{display:block;text-align:center;margin-top:16px;font-size:12px;color:var(--txt-m);text-decoration:none}
.voltar:hover{color:var(--coral)}
</style>
</head>
<body>
<div class="card">
  <div class="mark">BOAONDA</div>
  <h1>Cadastrar novo cliente</h1>
  <p class="sub">Preenchido por você em nome do cliente — o aceite dos termos deve ser confirmado com ele antes de marcar a caixa abaixo.</p>
  {% if erro %}<div class="erro">{{ erro }}</div>{% endif %}
  <form method="POST">
    <label>Nome completo *</label>
    <input type="text" name="nome" required autofocus/>
    <label>Empresa</label>
    <input type="text" name="empresa"/>
    <label>CNPJ *</label>
    <input type="text" name="cnpj" id="cnpj" placeholder="00.000.000/0000-00" maxlength="18" required/>
    <div class="row">
      <div>
        <label>Telefone *</label>
        <input type="text" name="telefone" required/>
      </div>
      <div>
        <label>E-mail *</label>
        <input type="email" name="email" required/>
      </div>
    </div>
    <div class="row">
      <div>
        <label>Cidade</label>
        <input type="text" name="cidade"/>
      </div>
      <div>
        <label>UF</label>
        <input type="text" name="uf" maxlength="2" style="text-transform:uppercase"/>
      </div>
    </div>
    <div class="termos">
      <input type="checkbox" name="aceite_termos" id="aceite" required/>
      <label for="aceite">Confirmo que o cliente concorda com o uso dos seus dados para contato comercial da BOAONDA, conforme a política de privacidade.</label>
    </div>
    <button class="btn" type="submit">Cadastrar e digitar pedido</button>
  </form>
  <a class="voltar" href="/catalogo/representante/painel">← Voltar aos meus clientes</a>
</div>
<script>
document.getElementById('cnpj').addEventListener('input', function(){
  let v = this.value.replace(/\\D/g,'').substring(0,14);
  if      (v.length > 12) v = v.replace(/^(\\d{2})(\\d{3})(\\d{3})(\\d{4})(\\d{0,2})$/,'$1.$2.$3/$4-$5');
  else if (v.length >  8) v = v.replace(/^(\\d{2})(\\d{3})(\\d{3})(\\d{0,4})$/,'$1.$2.$3/$4');
  else if (v.length >  5) v = v.replace(/^(\\d{2})(\\d{3})(\\d{0,3})$/,'$1.$2.$3');
  else if (v.length >  2) v = v.replace(/^(\\d{2})(\\d{0,3})$/,'$1.$2');
  this.value = v;
});
</script>
</body>
</html>'''


@app.route('/catalogo/representante/entrar', methods=['POST'])
def catalogo_representante_entrar():
    """Login do representante (e-mail + senha) — cadastro é feito só pelo
    admin em /admin/catalogo-representantes."""
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute(
            "SELECT id, nome, senha_hash, ativo FROM catalogo_representantes WHERE lower(email) = %s",
            (email,)
        )
        row = cursor.fetchone()
        conexao.close()
    except Exception as ex:
        return render_template_string(_CATALOGO_ENTRAR_HTML, erro=None, tab_ativa='representante',
                                       erro_rep=f'Não foi possível validar o login agora. ({ex})')
    if not row or not row[3] or not check_password_hash(row[2], senha):
        return render_template_string(_CATALOGO_ENTRAR_HTML, erro=None, tab_ativa='representante',
                                       erro_rep='E-mail ou senha incorretos.')
    session['catalogo_rep_id'] = str(row[0])
    session['catalogo_rep_nome'] = row[1]
    session.pop('catalogo_cadastro_id', None)
    session.pop('catalogo_cliente', None)
    return redirect(url_for('catalogo_representante_painel'))


@app.route('/catalogo/representante/sair')
def catalogo_representante_sair():
    """Logout completo do representante — encerra também qualquer cliente
    em cujo nome ele estivesse atuando no momento."""
    session.pop('catalogo_rep_id', None)
    session.pop('catalogo_rep_nome', None)
    session.pop('catalogo_cadastro_id', None)
    session.pop('catalogo_cliente', None)
    return redirect(url_for('catalogo_entrar'))


@app.route('/catalogo/representante/painel')
def catalogo_representante_painel():
    if not session.get('catalogo_rep_id'):
        return redirect(url_for('catalogo_entrar'))
    erro = request.args.get('erro')
    clientes = []
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cnpj, cidade, uf, criado_em
            FROM catalogo_cadastros
            WHERE representante_id = %s
            ORDER BY criado_em DESC
        """, (session['catalogo_rep_id'],))
        cols = [d[0] for d in cursor.description]
        linhas = cursor.fetchall()
        ids_raw = [row[0] for row in linhas]  # uuid.UUID original, p/ o JOIN de pedidos abaixo
        clientes = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                    for row in linhas]

        resumo_pedidos = {}
        if ids_raw:
            cursor.execute("""
                SELECT cadastro_id, COUNT(*), MAX(criado_em)
                FROM catalogo_pedidos
                WHERE cadastro_id = ANY(%s)
                GROUP BY cadastro_id
            """, (ids_raw,))
            for cadastro_id, total, ultimo in cursor.fetchall():
                resumo_pedidos[str(cadastro_id)] = {
                    'total': total,
                    'ultimo': _catalogo_valor_json_seguro(ultimo),
                }
        conexao.close()

        for c in clientes:
            r = resumo_pedidos.get(c['id'])
            c['total_pedidos'] = r['total'] if r else 0
            data_iso = (r['ultimo'] or '')[:10] if r else None
            c['ultimo_pedido'] = '/'.join(reversed(data_iso.split('-'))) if data_iso else None
    except Exception as ex:
        erro = erro or f'Não foi possível carregar seus clientes agora. ({ex})'
    return render_template_string(_CATALOGO_REP_PAINEL_HTML, clientes=clientes,
                                   rep_nome=session.get('catalogo_rep_nome'), erro=erro)


@app.route('/catalogo/representante/atuar/<cadastro_id>')
def catalogo_representante_atuar(cadastro_id):
    """Coloca a sessão no contexto do cliente escolhido — só permite se o
    cliente pertence à carteira deste representante (representante_id
    bate com a sessão), nunca por confiança no valor recebido na URL."""
    if not session.get('catalogo_rep_id'):
        return redirect(url_for('catalogo_entrar'))
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, representante
            FROM catalogo_cadastros
            WHERE id = %s AND representante_id = %s
        """, (cadastro_id, session['catalogo_rep_id']))
        row = cursor.fetchone()
        conexao.close()
    except Exception as ex:
        return redirect(url_for('catalogo_representante_painel', erro=str(ex)))
    if not row:
        return redirect(url_for('catalogo_representante_painel',
                                 erro='Cliente não encontrado ou fora da sua carteira.'))
    session['catalogo_cadastro_id'] = str(row[0])
    session['catalogo_cliente'] = {'nome': row[1], 'empresa': row[2] or '', 'representante': row[3]}
    return redirect(url_for('catalogo'))


@app.route('/catalogo/representante/cadastrar-cliente', methods=['GET', 'POST'])
def catalogo_representante_cadastrar_cliente():
    if not session.get('catalogo_rep_id'):
        return redirect(url_for('catalogo_entrar'))
    erro = None
    if request.method == 'POST':
        nome     = request.form.get('nome', '').strip()
        empresa  = request.form.get('empresa', '').strip() or None
        cnpj     = re.sub(r'\D', '', request.form.get('cnpj', ''))
        telefone = request.form.get('telefone', '').strip()
        email    = request.form.get('email', '').strip()
        cidade   = request.form.get('cidade', '').strip() or None
        uf       = request.form.get('uf', '').strip() or None
        aceite   = request.form.get('aceite_termos') == 'on'
        if not (nome and telefone and email and len(cnpj) == 14):
            erro = 'Preencha nome, telefone, e-mail e um CNPJ válido.'
        elif not aceite:
            erro = 'É necessário confirmar o aceite dos termos para continuar.'
        else:
            try:
                conexao = _conectar_catalogo_db()
                cursor = conexao.cursor()
                cursor.execute("""
                    INSERT INTO catalogo_cadastros
                        (nome, empresa, cnpj, telefone, email, cidade, uf, representante, representante_id, aceite_termos)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (nome, empresa, cnpj, telefone, email, cidade, uf,
                      session.get('catalogo_rep_nome'), session['catalogo_rep_id'], aceite))
                novo_id = cursor.fetchone()[0]
                conexao.commit()
                conexao.close()
                session['catalogo_cadastro_id'] = str(novo_id)
                session['catalogo_cliente'] = {'nome': nome, 'empresa': empresa or '',
                                                'representante': session.get('catalogo_rep_nome')}
                return redirect(url_for('catalogo'))
            except Exception as ex:
                if 'unique' in str(ex).lower() or 'duplicate' in str(ex).lower():
                    erro = 'Esse CNPJ já está cadastrado — volte e procure o cliente na sua lista.'
                else:
                    erro = f'Erro ao cadastrar: {ex}'
    return render_template_string(_CATALOGO_REP_CADASTRO_CLIENTE_HTML, erro=erro)


@app.route('/api/catalogo/pedido', methods=['POST'])
def api_catalogo_pedido():
    """Chamada pelo catalogo.html (dentro de idConfirmar/gerarPDF) para
    gravar a intenção de compra no banco, além do PDF já gerado localmente."""
    cadastro_id = session.get('catalogo_cadastro_id')
    if not cadastro_id:
        if session.get('catalogo_rep_id'):
            return jsonify({'erro': 'Selecione ou cadastre um cliente para fechar este pedido.',
                             'sem_cliente': True}), 409
        return jsonify({'erro': 'Sessão do catálogo expirada. Recarregue a página.'}), 401

    payload      = request.get_json(silent=True) or {}
    itens        = payload.get('itens') or []
    observacoes  = (payload.get('observacoes') or '').strip() or None

    if not itens:
        return jsonify({'erro': 'Carrinho vazio.'}), 400

    representante_responsavel = session.get('catalogo_rep_nome') if session.get('catalogo_rep_id') else None

    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            INSERT INTO catalogo_pedidos (cadastro_id, observacoes, representante_responsavel)
            VALUES (%s, %s, %s) RETURNING id
        """, (cadastro_id, observacoes, representante_responsavel))
        pedido_id = cursor.fetchone()[0]
        for item in itens:
            cursor.execute("""
                INSERT INTO catalogo_pedidos_itens
                    (pedido_id, produto_referencia, produto_nome, grade_tamanho, quantidade)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                pedido_id,
                item.get('referencia'),
                item.get('nome'),
                item.get('grade'),
                item.get('quantidade'),
            ))
        conexao.commit()
        conexao.close()
        return jsonify({'status': 'ok', 'pedido_id': str(pedido_id)})
    except Exception as ex:
        return jsonify({'erro': f'Erro ao salvar o pedido: {ex}'}), 500


def _catalogo_valor_json_seguro(v):
    """psycopg2 devolve uuid.UUID (colunas id) e datetime (colunas *_em) —
    nenhum dos dois é serializável por jsonify() sem conversão. Números,
    texto e bool passam direto; qualquer outra coisa vira string."""
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


@app.route('/api/catalogo/quem-sou-eu')
def api_catalogo_quem_sou_eu():
    """Identidade da sessão atual do catálogo público — usado pelo header
    do catalogo.html pra mostrar "logado como X" e habilitar o botão de
    troca. catalogo.html é servido como arquivo estático (send_from_directory),
    por isso a identidade chega via fetch em vez de Jinja."""
    if session.get('catalogo_rep_id') and not session.get('catalogo_cadastro_id'):
        # Representante navegando no catálogo sem ter escolhido um cliente
        # ainda — estado válido agora, só barra na hora de fechar pedido.
        return jsonify({'logado': True, 'cliente': {}, 'rep_nome': session.get('catalogo_rep_nome'),
                         'sem_cliente': True})
    if not session.get('catalogo_cadastro_id'):
        return jsonify({'logado': False})
    return jsonify({
        'logado': True,
        'cliente': session.get('catalogo_cliente') or {},
        'rep_nome': session.get('catalogo_rep_nome'),
    })


@app.route('/api/catalogo/meu-historico')
def api_catalogo_meu_historico():
    """Pedidos do PRÓPRIO cadastro logado na sessão (nunca de outro CNPJ) —
    histórico self-service exibido no catalogo.html, escopado sempre por
    catalogo_cadastro_id da sessão, nunca por parâmetro vindo do cliente."""
    cadastro_id = session.get('catalogo_cadastro_id')
    if not cadastro_id:
        return jsonify({'erro': 'Sessão do catálogo expirada. Recarregue a página.'}), 401
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            SELECT id, status, observacoes, criado_em
            FROM catalogo_pedidos
            WHERE cadastro_id = %s
            ORDER BY criado_em DESC
            LIMIT 200
        """, (cadastro_id,))
        cols = [d[0] for d in cursor.description]
        linhas = cursor.fetchall()
        pedido_ids_raw = [row[0] for row in linhas]  # uuid.UUID original, p/ usar no JOIN abaixo
        pedidos = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                   for row in linhas]

        itens_por_pedido = {}
        if pedido_ids_raw:
            cursor.execute("""
                SELECT pedido_id, produto_referencia, produto_nome, grade_tamanho, quantidade
                FROM catalogo_pedidos_itens
                WHERE pedido_id = ANY(%s)
                ORDER BY criado_em
            """, (pedido_ids_raw,))
            cols = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                item = dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                itens_por_pedido.setdefault(item['pedido_id'], []).append(item)
        conexao.close()

        for p in pedidos:
            p['itens'] = itens_por_pedido.get(p['id'], [])
        return jsonify({'disponivel': True, 'pedidos': pedidos})
    except Exception as ex:
        return jsonify({'disponivel': False, 'erro': str(ex)}), 500


@app.route('/api/catalogo/resumo')
def api_catalogo_resumo():
    """KPI leve pro nó "Cadastros do Catálogo" na Home — só contagens do
    mês. Nunca derruba a Home: qualquer falha volta como disponivel=False
    (200), pro card mostrar "–" em vez de quebrar o carregamento da página."""
    usuario = _achar_usuario(session.get('username'))
    if 'catalogo_leads' not in _modulos_do_usuario(usuario):
        return jsonify({'disponivel': False, 'erro': 'sem acesso'}), 403
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("SELECT COUNT(*) FROM catalogo_cadastros")
        total_cadastros = cursor.fetchone()[0]
        cursor.execute("""
            SELECT COUNT(*) FROM catalogo_cadastros
            WHERE date_trunc('month', criado_em) = date_trunc('month', now())
        """)
        cadastros_mes = cursor.fetchone()[0]
        cursor.execute("""
            SELECT COUNT(*) FROM catalogo_pedidos
            WHERE date_trunc('month', criado_em) = date_trunc('month', now())
        """)
        pedidos_mes = cursor.fetchone()[0]
        conexao.close()
        return jsonify({
            'disponivel': True,
            'total_cadastros': total_cadastros,
            'cadastros_mes': cadastros_mes,
            'pedidos_mes': pedidos_mes,
        })
    except Exception:
        return jsonify({'disponivel': False})


@app.route('/api/catalogo/dados')
def api_catalogo_dados():
    """Dados completos (cadastros, pedidos, itens) pro dashboard "Cadastros
    do Catálogo" — consulta o Supabase ao vivo a cada chamada, diferente dos
    demais módulos (que leem JSON pré-gerado pelo processador.py a partir do
    MySQL). Volume esperado é baixo (leads do catálogo público), por isso a
    consulta direta é aceitável sem uma camada de cache."""
    usuario = _achar_usuario(session.get('username'))
    if 'catalogo_leads' not in _modulos_do_usuario(usuario):
        return jsonify({'erro': 'Você não tem acesso a este módulo.'}), 403
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()

        cursor.execute("""
            SELECT id, nome, empresa, cnpj, telefone, email, cidade, uf,
                   representante, criado_em
            FROM catalogo_cadastros
            ORDER BY criado_em DESC
            LIMIT 500
        """)
        cols = [d[0] for d in cursor.description]
        cadastros = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                     for row in cursor.fetchall()]

        cursor.execute("""
            SELECT p.id, p.cadastro_id, p.status, p.representante_responsavel,
                   p.observacoes, p.criado_em,
                   c.nome AS cli_nome, c.empresa AS cli_empresa, c.cnpj AS cli_cnpj
            FROM catalogo_pedidos p
            LEFT JOIN catalogo_cadastros c ON c.id = p.cadastro_id
            ORDER BY p.criado_em DESC
            LIMIT 500
        """)
        cols = [d[0] for d in cursor.description]
        pedidos = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                   for row in cursor.fetchall()]

        cursor.execute("""
            SELECT id, pedido_id, produto_referencia, produto_nome,
                   grade_tamanho, quantidade, criado_em
            FROM catalogo_pedidos_itens
            ORDER BY criado_em DESC
            LIMIT 2000
        """)
        cols = [d[0] for d in cursor.description]
        itens = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                 for row in cursor.fetchall()]

        conexao.close()
        return jsonify({
            'disponivel': True,
            'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'cadastros': cadastros,
            'pedidos': pedidos,
            'itens': itens,
        })
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'disponivel': False, 'erro': str(ex)}), 500


@app.route('/<path:filename>')
def serve_file(filename):
    # Bloqueia o HTML do dashboard e os JSONs de dados de módulos que o
    # usuário não tem permissão de ver — mesmo que ele digite a URL direto
    # (a trava não é só visual no portal, ver aplicarRestricoesModulos no JS).
    modulo_key = _ARQUIVO_PARA_MODULO.get(filename)
    if modulo_key:
        usuario = _achar_usuario(session.get('username'))
        if modulo_key not in _modulos_do_usuario(usuario):
            return jsonify({'erro': f'Você não tem acesso ao módulo '
                                     f'"{MODULOS[modulo_key]["label"]}". '
                                     f'Fale com um administrador.'}), 403
    # JSONs gerados pelo /upload vivem em DATA_DIR (volume persistente);
    # o restante (HTML/CSS/JS dos dashboards) vem do código versionado.
    if filename in DATA_FILES:
        resp = send_from_directory(DATA_DIR, filename)
        # JSONs públicos do catálogo: cache de 5 min no browser (reduz carga
        # em acessos simultâneos; TTL curto garante atualização pós-upload).
        if filename in DATA_FILES_PUBLICOS:
            resp.cache_control.public = True
            resp.cache_control.max_age = 300
        return resp
    if filename in PAGINAS_COM_SIDEBAR_VENDAS:
        return render_template(filename)
    return send_from_directory(FRONTEND_DIR, filename)


# ─────────────────────────────────────────────
#  PAINEL DE CONFIGURAÇÕES — hub com sidebar fixa (admin-only)
# ─────────────────────────────────────────────
# Cada seção é uma página que já existe e já funciona sozinha (upload,
# fotos, home do catálogo, config de produção, usuários, diagnóstico,
# recarregar) — o painel só empresta uma casca comum (sidebar + iframe),
# sem reescrever a lógica de nenhuma delas.
CONFIG_SECOES = [
    {'id': 'upload',     'icone': '⟳',  'label': 'Atualizar dados',              'url': '/upload'},
    {'id': 'metas',      'icone': '🎯', 'label': 'Metas comerciais',             'url': '/admin/metas'},
    {'id': 'fotos',      'icone': '🖼', 'label': 'Atualizar fotos do catálogo',   'url': '/admin/fotos'},
    {'id': 'home',       'icone': '🏠', 'label': 'Editar home do catálogo',       'url': '/admin/home'},
    {'id': 'usuarios',   'icone': '👤', 'label': 'Gerenciar usuários',            'url': '/admin/usuarios'},
    {'id': 'reps_cat',   'icone': '🧑‍💼', 'label': 'Representantes do catálogo',   'url': '/admin/catalogo-representantes'},
    {'id': 'producao',   'icone': '⚙',  'label': 'Configurações de produção',     'url': '/config'},
    {'id': 'diag',       'icone': '🩺', 'label': 'Diagnóstico da fonte de dados', 'url': '/admin/diag'},
    {'id': 'recarregar', 'icone': '🔄', 'label': 'Recarregar dados no servidor',  'url': '/admin/recarregar'},
]

_CONFIGURACOES_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Configurações</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
html,body{height:100%}
body{background:var(--bg);color:var(--verde-dark);display:flex;flex-direction:column;overflow:hidden}
.topbar{background:var(--card);border-bottom:1px solid var(--line);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.brand{font-size:16px;font-weight:800;color:var(--coral);letter-spacing:1.5px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:12px;margin-left:6px;letter-spacing:1px}
.back{font-size:11px;color:var(--txt-s);text-decoration:none;border:1px solid var(--line);border-radius:6px;padding:6px 12px}
.back:hover{color:var(--coral);border-color:var(--coral)}
.layout{flex:1;display:flex;min-height:0}
.sidebar{width:250px;flex-shrink:0;background:var(--card);border-right:1px solid var(--line);overflow-y:auto;padding:16px 0}
.side-title{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--txt-m);padding:0 20px 10px}
.side-item{display:flex;align-items:center;gap:10px;padding:11px 20px;font-size:12.5px;font-weight:600;color:var(--verde-dark);cursor:pointer;border-left:3px solid transparent;transition:.12s}
.side-item:hover{background:rgba(237,104,66,.06)}
.side-item.active{background:rgba(237,104,66,.1);border-left-color:var(--coral);color:var(--coral)}
.side-icon{font-size:15px;width:20px;text-align:center}
.content{flex:1;min-width:0;position:relative}
.content iframe{width:100%;height:100%;border:none;display:block}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">BOAONDA <span>· Configurações</span></div>
  <a class="back" href="/" target="_top">← Voltar ao portal</a>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="side-title">Configurações</div>
    {% for s in secoes %}
    <div class="side-item" data-id="{{ s.id }}" onclick="abrirSecao('{{ s.id }}')">
      <span class="side-icon">{{ s.icone }}</span><span>{{ s.label }}</span>
    </div>
    {% endfor %}
  </div>
  <div class="content">
    <iframe id="cfg-iframe" src=""></iframe>
  </div>
</div>
<script>
const SECOES = {{ secoes|tojson }};
function abrirSecao(id){
  const sec = SECOES.find(s=>s.id===id);
  if(!sec) return;
  document.getElementById('cfg-iframe').src = sec.url;
  document.querySelectorAll('.side-item').forEach(el=>el.classList.toggle('active', el.dataset.id===id));
  history.replaceState(null, '', '/admin/configuracoes?secao='+id);
}
abrirSecao(new URLSearchParams(location.search).get('secao') || 'upload');
</script>
</body>
</html>'''


@app.route('/admin/configuracoes')
def admin_configuracoes():
    return render_template_string(_CONFIGURACOES_HTML, secoes=CONFIG_SECOES)


_UPLOAD_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Atualizar dados</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:560px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.field{margin-bottom:18px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin-bottom:8px}
.hint{font-size:11px;color:var(--txt-m);margin-top:4px}
input[type=file]{width:100%;font-size:12px;color:var(--txt-s)}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
.spinner{display:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Atualizar dados</h1>
  <p class="sub">Envie os arquivos exportados do ERP para regenerar os dashboards (Vendas, Programação, Estoque).</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message|safe }}</div>
  {% endif %}

  <form class="card" method="post" enctype="multipart/form-data" onsubmit="document.getElementById('btn').textContent='Processando... isso pode levar alguns minutos';document.getElementById('btn').disabled=true">
    <div class="field">
      <label>3YS.csv (vendas e programação)</label>
      <input type="file" name="arquivo_3ys" accept=".csv,.ods">
      <div class="hint">Opcional — se não enviado, vendas e programação mantêm os dados anteriores. Arquivo pode ter ~130MB, o envio pode demorar.</div>
    </div>
    <div class="field">
      <label>ESQT — estoque PA (.csv ou .xls)</label>
      <input type="file" name="arquivo_esqt" accept=".csv,.xls">
      <div class="hint">Obrigatório. Aceita exportação em CSV (recomendado) ou XLS.</div>
    </div>
    <button class="btn" id="btn" type="submit">Processar e atualizar dashboards</button>
  </form>

  <a class="back" href="/" target="_top">← Voltar ao portal</a>
  &nbsp;·&nbsp;
  <a class="back" href="/admin/configuracoes" target="_top">← Voltar às Configurações</a>
</div>
</body>
</html>'''


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'GET':
        return render_template_string(_UPLOAD_HTML, message=None, ok=True)

    f_3ys  = request.files.get('arquivo_3ys')
    f_esqt = request.files.get('arquivo_esqt')

    if (not f_esqt or not f_esqt.filename) and not (DATA_DIR / 'dados_estoque.json').exists():
        return render_template_string(_UPLOAD_HTML, message='Envie ao menos o ESQT.xls na primeira atualização.', ok=False)

    path_3ys = path_esqt = None
    try:
        if f_3ys and f_3ys.filename:
            path_3ys = UPLOADS_DIR / f_3ys.filename
            f_3ys.save(path_3ys)

        if f_esqt and f_esqt.filename:
            path_esqt = UPLOADS_DIR / f_esqt.filename
            f_esqt.save(path_esqt)
        else:
            # Mantém estoque atual — busca o backup salvo (CSV ou XLS)
            path_esqt = next(
                (UPLOADS_DIR / f'_ESQT_atual{ext}' for ext in ('.csv', '.xls')
                 if (UPLOADS_DIR / f'_ESQT_atual{ext}').exists()),
                None
            )
            if not path_esqt:
                return render_template_string(_UPLOAD_HTML, message='ESQT não encontrado para reprocessar. Envie o arquivo.', ok=False)

        resumo = processador.processar_tudo(
            arquivo_3ys=str(path_3ys) if path_3ys else None,
            arquivo_esqt=str(path_esqt),
            output_dir=str(DATA_DIR),
        )

        # Mix programado mudou — recalcula ocupação/eficiência contra a
        # capacidade atual (não bloqueia o upload se ainda não houver
        # dados_capacidade.json ou dados_programacao_detalhe.json).
        try:
            import calculo_ocupacao_semanal
            calculo_ocupacao_semanal.gerar(diretorio=str(DATA_DIR))
        except Exception:
            traceback.print_exc()

        # Guarda uma cópia do ESQT mais recente para reprocessamentos futuros
        if f_esqt and f_esqt.filename:
            esqt_ext = Path(f_esqt.filename).suffix.lower()
            (UPLOADS_DIR / f'_ESQT_atual{esqt_ext}').write_bytes(path_esqt.read_bytes())

        cm = resumo['vendas_mes']
        t  = resumo['estoque_totais']
        diag = resumo.get('diag_vendas', {})
        msg = (f"Dados atualizados em {resumo['gerado_em']}.<br>"
               f"Estoque livre: {t['livre']:,}".replace(',', '.') +
               f" | Vendas {resumo['mes_label']}: MI {cm.get('MI',0):,} ME {cm.get('ME',0):,}".replace(',', '.'))
        if diag:
            anomes_ok = diag.get('mes_atual_ok', True)
            anomes_str = ', '.join(diag.get('anomes_presentes', []))
            if not anomes_ok:
                msg += (f"<br><b style='color:#e55'>⚠ Atenção: o mês {resumo['mes_label']} não foi encontrado no CSV. "
                        f"Anomes presentes: {anomes_str or '(nenhum)'}. "
                        f"Verifique se o campo AAMM do CSV está no formato AAAAMM (ex: 202607).</b>")
            else:
                msg += f"<br><small style='color:#888'>Anomes no CSV: {anomes_str}</small>"
        return render_template_string(_UPLOAD_HTML, message=msg, ok=True)

    except Exception as ex:
        traceback.print_exc()
        return render_template_string(_UPLOAD_HTML, message=f'Erro ao processar: {ex}', ok=False)
    finally:
        # 3YS é grande — não manter no disco após processar
        if path_3ys and path_3ys.exists():
            path_3ys.unlink()


_CONFIG_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Configurações</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:560px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.field{margin-bottom:18px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin-bottom:8px}
.hint{font-size:11px;color:var(--txt-m);margin-top:4px}
input[type=number]{width:100%;background:#f3f0eb;border:1px solid var(--line);border-radius:8px;padding:10px 14px;color:var(--verde-dark);font-size:14px;outline:none}
input[type=number]:focus{border-color:var(--coral)}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Configurações de produção</h1>
  <p class="sub">Esses parâmetros alimentam os cálculos de prazo dos dashboards (Carteira, Programação) sem precisar reprocessar os dados.</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message }}</div>
  {% endif %}

  <form class="card" method="post">
    <div class="field">
      <label>Prazo produtivo (dias)</label>
      <input type="number" name="prazo_producao_dias" min="1" max="365" value="{{ prazo }}" required>
      <div class="hint">Lead time médio de produção. Usado para calcular se um pedido em carteira ainda tem tempo hábil de produção (hoje + prazo &gt; entrega prevista = em atraso) e para destacar a semana de referência na Programação.</div>
    </div>
    <div class="field">
      <label>Limite mensal de gasto da IA (US$)</label>
      <input type="number" name="ia_limite_mensal_usd" min="0" max="100000" step="0.01" value="{{ ia_limite }}" required>
      <div class="hint">Teto de gasto mensal da aba Inteligência (chat com IA). Ao atingir, novas perguntas são bloqueadas até virar o mês ou aumentar o teto. Use 0 para não bloquear. Recomenda-se também definir um limite de gasto no Console da Anthropic como rede de segurança.</div>
    </div>
    <button class="btn" type="submit">Salvar</button>
  </form>

  <a class="back" href="/" target="_top">← Voltar ao portal</a>
</div>
</body>
</html>'''


@app.route('/config', methods=['GET', 'POST'])
def config():
    config_path = DATA_DIR / 'config_producao.json'
    try:
        with open(config_path, encoding='utf-8') as f_:
            cfg = json.load(f_)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    prazo = cfg.get('prazo_producao_dias', 45)
    ia_limite = cfg.get('ia_limite_mensal_usd', IA_LIMITE_PADRAO_USD)

    message, ok = None, True
    if request.method == 'POST':
        try:
            novo_prazo = int(request.form.get('prazo_producao_dias', ''))
            if novo_prazo < 1 or novo_prazo > 365:
                raise ValueError('prazo')
            novo_limite = float(request.form.get('ia_limite_mensal_usd', ''))
            if novo_limite < 0 or novo_limite > 100000:
                raise ValueError('limite')
        except ValueError as ve:
            message = ('Informe um número de dias entre 1 e 365.' if str(ve) == 'prazo'
                       else 'Informe um limite de gasto válido (0 a 100000).')
            ok = False
        else:
            prazo, ia_limite = novo_prazo, round(novo_limite, 2)
            cfg['prazo_producao_dias'] = prazo
            cfg['ia_limite_mensal_usd'] = ia_limite
            with open(config_path, 'w', encoding='utf-8') as f_:
                json.dump(cfg, f_, ensure_ascii=False, indent=2)
            message = f'Configuração salva: prazo {prazo} dias · limite IA US$ {ia_limite:.2f}/mês.'

    return render_template_string(_CONFIG_HTML, message=message, ok=ok, prazo=prazo, ia_limite=ia_limite)


# ─────────────────────────────────────────────
#  GESTÃO DE USUÁRIOS (admin-only)
# ─────────────────────────────────────────────
_USUARIOS_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Usuários</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:760px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
h2{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin:28px 0 12px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.field{margin-bottom:16px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin-bottom:8px}
input[type=text],input[type=password],select{width:100%;background:#f3f0eb;border:1px solid var(--line);border-radius:8px;padding:10px 14px;color:var(--verde-dark);font-size:13px;outline:none;font-family:'Montserrat',sans-serif}
input:focus,select:focus{border-color:var(--coral)}
.hint{font-size:11px;color:var(--txt-m);margin-top:4px}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:12px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.btn-sm{padding:7px 12px;font-size:11px}
.btn-danger{background:transparent;color:#c0392b;border:1px solid rgba(192,57,43,.35)}
.btn-danger:hover{background:rgba(192,57,43,.08)}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt-m);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);font-size:12px;vertical-align:middle}
tr:last-child td{border-bottom:none}
.you{font-size:9px;color:var(--coral);font-weight:700;margin-left:6px}
.row-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.row-actions form{display:flex;gap:6px;align-items:center;margin:0}
.row-actions input[type=password]{width:130px;padding:6px 10px;font-size:11px}
.row-actions select{width:auto;padding:6px 8px;font-size:11px}
.modulos-row td{border-bottom:1px solid var(--line);padding-top:0}
.modulos-form{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;background:#f8f5f1;border-radius:8px;padding:10px 12px}
.modulos-form .lbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--txt-m)}
.chk{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--verde-dark);margin:0}
.chk input{width:auto}
.modulos-criar{display:flex;flex-wrap:wrap;gap:10px 16px}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Gerenciar usuários</h1>
  <p class="sub">Cadastre e administre quem acessa o portal. Perfil "Admin" tem acesso total (inclui atualizar dados, fotos e esta tela); perfil "Comum" só vê os dashboards liberados abaixo, módulo por módulo.</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message }}</div>
  {% endif %}

  <div class="card">
    <table>
      <thead><tr><th>Usuário</th><th>Perfil</th><th>Criado em</th><th>Ações</th></tr></thead>
      <tbody>
        {% for u in usuarios %}
        <tr>
          <td>{{ u.username }}{% if u.username == usuario_atual %}<span class="you">você</span>{% endif %}</td>
          <td>{{ 'Admin' if u.role == 'admin' else 'Comum' }}</td>
          <td>{{ u.criado_em or '–' }}</td>
          <td>
            <div class="row-actions">
              <form method="post" action="/admin/usuarios/role">
                <input type="hidden" name="username" value="{{ u.username }}">
                <select name="role">
                  <option value="comum" {{ 'selected' if u.role != 'admin' else '' }}>Comum</option>
                  <option value="admin" {{ 'selected' if u.role == 'admin' else '' }}>Admin</option>
                </select>
                <button class="btn btn-sm" type="submit">Salvar</button>
              </form>
              <form method="post" action="/admin/usuarios/senha">
                <input type="hidden" name="username" value="{{ u.username }}">
                <input type="password" name="nova_senha" placeholder="Nova senha" minlength="6" required>
                <button class="btn btn-sm" type="submit">Trocar</button>
              </form>
              <form method="post" action="/admin/usuarios/remover"
                    onsubmit="return confirm('Remover o usuário {{ u.username }}? Essa ação não pode ser desfeita.')">
                <input type="hidden" name="username" value="{{ u.username }}">
                <button class="btn btn-sm btn-danger" type="submit">Remover</button>
              </form>
            </div>
          </td>
        </tr>
        {% if u.role == 'admin' %}
        <tr class="modulos-row"><td colspan="4"><span class="hint">Admin — acesso total a todos os módulos.</span></td></tr>
        {% else %}
        <tr class="modulos-row">
          <td colspan="4">
            <form method="post" action="/admin/usuarios/modulos" class="modulos-form">
              <input type="hidden" name="username" value="{{ u.username }}">
              <span class="lbl">Módulos liberados:</span>
              {% for mk, mv in modulos_registro.items() %}
              <label class="chk"><input type="checkbox" name="modulos" value="{{ mk }}"
                {{ 'checked' if mk in u.modulos_efetivos else '' }}> {{ mv.label }}</label>
              {% endfor %}
              <button class="btn btn-sm" type="submit">Salvar módulos</button>
            </form>
          </td>
        </tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
  </div>

  <h2>Novo usuário</h2>
  <form class="card" method="post" action="/admin/usuarios/criar">
    <div class="field">
      <label>Usuário</label>
      <input type="text" name="username" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" required>
    </div>
    <div class="field">
      <label>Senha</label>
      <input type="password" name="senha" minlength="6" required>
      <div class="hint">Mínimo 6 caracteres.</div>
    </div>
    <div class="field">
      <label>Perfil</label>
      <select name="role" id="novo-role" onchange="document.getElementById('novo-modulos').style.display=this.value==='admin'?'none':'block'">
        <option value="comum">Comum — só acessa os dashboards</option>
        <option value="admin">Admin — acesso total (dados, fotos, usuários)</option>
      </select>
    </div>
    <div class="field" id="novo-modulos">
      <label>Módulos liberados (perfil Comum)</label>
      <div class="modulos-criar">
        {% for mk, mv in modulos_registro.items() %}
        <label class="chk"><input type="checkbox" name="modulos" value="{{ mk }}" checked> {{ mv.label }}</label>
        {% endfor %}
      </div>
      <div class="hint">Desmarque os módulos que este usuário não deve acessar. Ignorado se o perfil for Admin.</div>
    </div>
    <button class="btn" type="submit">Criar usuário</button>
  </form>

  <a class="back" href="/" target="_top">← Voltar ao portal</a>
</div>
</body>
</html>'''


def _redirect_usuarios(msg, ok=True):
    return redirect(url_for('admin_usuarios', msg=msg, ok='1' if ok else '0'))


@app.route('/admin/usuarios')
def admin_usuarios():
    msg = request.args.get('msg')
    ok = request.args.get('ok', '1') == '1'
    usuarios = sorted(_ler_usuarios(), key=lambda u: u['username'].lower())
    for u in usuarios:
        u['modulos_efetivos'] = sorted(_modulos_do_usuario(u))
    return render_template_string(_USUARIOS_HTML, usuarios=usuarios, message=msg, ok=ok,
                                   usuario_atual=session.get('username'), modulos_registro=MODULOS)


@app.route('/admin/usuarios/criar', methods=['POST'])
def admin_usuarios_criar():
    username = request.form.get('username', '').strip()
    senha = request.form.get('senha', '')
    role = request.form.get('role', 'comum')
    if role not in ('admin', 'comum'):
        role = 'comum'
    modulos = [m for m in request.form.getlist('modulos') if m in MODULOS_KEYS]
    if not username or len(senha) < 6:
        return _redirect_usuarios('Informe um usuário e uma senha com pelo menos 6 caracteres.', ok=False)
    usuarios = _ler_usuarios()
    if any(u['username'].lower() == username.lower() for u in usuarios):
        return _redirect_usuarios(f'Já existe um usuário "{username}".', ok=False)
    usuarios.append({
        'username': username,
        'senha_hash': generate_password_hash(senha),
        'role': role,
        'modulos': modulos,
        'criado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
    })
    _salvar_usuarios(usuarios)
    return _redirect_usuarios(f'Usuário "{username}" criado com sucesso.')


@app.route('/admin/usuarios/senha', methods=['POST'])
def admin_usuarios_senha():
    username = request.form.get('username', '').strip()
    nova_senha = request.form.get('nova_senha', '')
    if len(nova_senha) < 6:
        return _redirect_usuarios('A nova senha precisa ter pelo menos 6 caracteres.', ok=False)
    usuarios = _ler_usuarios()
    usuario = next((u for u in usuarios if u['username'] == username), None)
    if not usuario:
        return _redirect_usuarios('Usuário não encontrado.', ok=False)
    usuario['senha_hash'] = generate_password_hash(nova_senha)
    _salvar_usuarios(usuarios)
    return _redirect_usuarios(f'Senha de "{username}" atualizada.')


@app.route('/admin/usuarios/role', methods=['POST'])
def admin_usuarios_role():
    username = request.form.get('username', '').strip()
    nova_role = request.form.get('role', '')
    if nova_role not in ('admin', 'comum'):
        return _redirect_usuarios('Perfil inválido.', ok=False)
    usuarios = _ler_usuarios()
    usuario = next((u for u in usuarios if u['username'] == username), None)
    if not usuario:
        return _redirect_usuarios('Usuário não encontrado.', ok=False)
    admins_restantes = [u for u in usuarios if u.get('role') == 'admin' and u['username'] != username]
    if usuario.get('role') == 'admin' and nova_role != 'admin' and not admins_restantes:
        return _redirect_usuarios('Não é possível rebaixar o último administrador do sistema.', ok=False)
    usuario['role'] = nova_role
    _salvar_usuarios(usuarios)
    return _redirect_usuarios(f'Perfil de "{username}" atualizado para {"Admin" if nova_role == "admin" else "Comum"}.')


@app.route('/admin/usuarios/modulos', methods=['POST'])
def admin_usuarios_modulos():
    username = request.form.get('username', '').strip()
    modulos = [m for m in request.form.getlist('modulos') if m in MODULOS_KEYS]
    usuarios = _ler_usuarios()
    usuario = next((u for u in usuarios if u['username'] == username), None)
    if not usuario:
        return _redirect_usuarios('Usuário não encontrado.', ok=False)
    if usuario.get('role') == 'admin':
        return _redirect_usuarios('Administradores sempre têm acesso a todos os módulos.', ok=False)
    usuario['modulos'] = modulos
    _salvar_usuarios(usuarios)
    labels = ', '.join(MODULOS[m]['label'] for m in modulos) if modulos else 'nenhum módulo'
    return _redirect_usuarios(f'Módulos de "{username}" atualizados: {labels}.')


@app.route('/admin/usuarios/remover', methods=['POST'])
def admin_usuarios_remover():
    username = request.form.get('username', '').strip()
    usuarios = _ler_usuarios()
    usuario = next((u for u in usuarios if u['username'] == username), None)
    if not usuario:
        return _redirect_usuarios('Usuário não encontrado.', ok=False)
    admins_restantes = [u for u in usuarios if u.get('role') == 'admin' and u['username'] != username]
    if usuario.get('role') == 'admin' and not admins_restantes:
        return _redirect_usuarios('Não é possível remover o último administrador do sistema.', ok=False)
    usuarios = [u for u in usuarios if u['username'] != username]
    _salvar_usuarios(usuarios)
    if session.get('username') == username:
        session.clear()
        return redirect(url_for('login'))
    return _redirect_usuarios(f'Usuário "{username}" removido.')


_RECARREGAR_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Recarregar dados</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:560px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Recarregar dados do último deploy</h1>
  <p class="sub">Copia os JSONs versionados no repositório (gerados localmente a
  partir do MySQL/ESQT e enviados via git push) para o volume persistente que
  alimenta o portal — use depois de cada atualização de dados.</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message }}</div>
  {% endif %}

  <form class="card" method="post">
    <button class="btn" type="submit" {{ 'disabled' if disabled else '' }}>Recarregar agora</button>
  </form>

  <a class="back" href="/" target="_top">← Voltar ao portal</a>
</div>
</body>
</html>'''


_METAS_ADMIN_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Metas comerciais</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:600px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px;line-height:1.6}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:16px}
.step-t{font-size:13px;font-weight:800;margin-bottom:6px;display:flex;align-items:center}
.step-t .n{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;background:var(--coral);color:#fff;font-size:11px;margin-right:8px}
.step-d{font-size:11.5px;color:var(--txt-s);margin-bottom:14px;line-height:1.6}
.btn{display:inline-block;background:var(--coral);color:#fff;border:none;border-radius:8px;padding:11px 20px;font-size:12.5px;font-weight:700;cursor:pointer;text-decoration:none}
.btn:hover{background:#dd7051}
.btn.sec{background:#fff;border:1px solid var(--line);color:var(--txt-s)}
.btn.sec:hover{border-color:var(--coral);color:var(--coral)}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.fname{font-size:11px;color:var(--txt-m);margin-top:10px;font-style:italic}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-top:14px;line-height:1.5;white-space:pre-wrap}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Metas comerciais</h1>
  <p class="sub">Registro formal das metas — uma planilha única com a <b>meta da empresa</b> e a meta de <b>cada representante</b>, por mês (todos os meses com vendas registradas). Ao importar, os quadros de Vendas (operação e por representante) passam a refletir estes valores.</p>

  <div class="card">
    <div class="step-t"><span class="n">1</span>Baixar planilha</div>
    <div class="step-d">Vem pré-preenchida com o que já está gravado: linha <b>META EMPRESA</b> no topo e uma linha por representante, colunas = meses.</div>
    <a class="btn" href="/api/metas/exportar">⬇ Baixar planilha</a>
  </div>

  <div class="card">
    <div class="step-t"><span class="n">2</span>Importar planilha</div>
    <div class="step-d">Edite as metas e reenvie. <b>A planilha é a fonte completa:</b> cada célula sobrescreve o valor gravado; célula em branco remove a meta. A soma das metas dos reps pode exceder a meta da empresa (spread).</div>
    <input type="file" id="file" accept=".xlsx" style="display:none" onchange="sel()">
    <button class="btn sec" onclick="document.getElementById('file').click()">📄 Escolher arquivo</button>
    <button class="btn" id="imp" onclick="importar()" disabled>⬆ Importar</button>
    <div class="fname" id="fname">Nenhum arquivo selecionado</div>
    <div class="msg" id="msg" style="display:none"></div>
  </div>

  <a class="back" href="/admin/configuracoes" target="_top">← Voltar às Configurações</a>
</div>
<script>
function sel(){ var f=document.getElementById('file').files[0]; document.getElementById('fname').textContent=f?f.name:'Nenhum arquivo selecionado'; document.getElementById('imp').disabled=!f; }
async function importar(){
  var f=document.getElementById('file').files[0]; if(!f) return;
  var b=document.getElementById('imp'); b.disabled=true; b.textContent='Importando…';
  var m=document.getElementById('msg');
  try{
    var fd=new FormData(); fd.append('arquivo',f);
    var r=await fetch('/api/metas/importar',{method:'POST',body:fd});
    var j=await r.json();
    m.style.display='block';
    if(j.status==='ok'){
      m.className='msg ok';
      m.textContent='✓ Metas gravadas — '+j.meses_meta_empresa+' mês(es) com meta da empresa, '+j.reps_com_meta+' representante(s) com meta, '+j.celulas+' célula(s). ('+j.gerado_em+')';
    }else{ m.className='msg err'; m.textContent='✗ '+(j.mensagem||'Erro ao importar.'); }
  }catch(e){ m.style.display='block'; m.className='msg err'; m.textContent='✗ Falha na comunicação: '+e; }
  b.disabled=false; b.textContent='⬆ Importar';
}
</script>
</body>
</html>'''


_FOTOS_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Atualizar fotos do catálogo</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:560px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:13px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.msg.run{background:rgba(237,104,66,.08);color:var(--coral);border:1px solid rgba(237,104,66,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
.progress-bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:12px}
.progress-fill{height:100%;background:var(--coral);border-radius:3px;width:0%;transition:width .5s}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid rgba(237,104,66,.3);
  border-top-color:var(--coral);border-radius:50%;animation:spin .8s linear infinite;margin-right:6px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.dl-link{display:inline-block;margin-top:10px;font-size:12px;font-weight:700;color:var(--coral);text-decoration:none}
.dl-link:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Atualizar fotos do catálogo</h1>
  <p class="sub">Busca as imagens de produto no Inside Boaonda e gera o arquivo de fotos usado pelo catálogo público.</p>

  <div id="status-box"></div>

  <div class="card" id="form-card">
    <button class="btn" id="btn" onclick="iniciarJob()">Buscar fotos e atualizar catálogo</button>
    <div class="progress-bar" id="pbar" style="display:none"><div class="progress-fill" id="pfill"></div></div>
  </div>

  <a class="back" href="/admin/configuracoes" target="_top">← Voltar às Configurações</a>
  &nbsp;·&nbsp;
  <a class="back" href="/catalogo" target="_blank">Ver catálogo público ↗</a>
</div>
<script>
let _poll = null;
let _elapsed = 0;

function iniciarJob() {
  document.getElementById('btn').disabled = true;
  document.getElementById('pbar').style.display = 'block';
  document.getElementById('status-box').innerHTML =
    '<div class="msg run"><span class="spinner"></span>Iniciando busca de fotos…</div>';
  fetch('/admin/fotos/start', {method:'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) { _elapsed = 0; _poll = setInterval(verificarStatus, 3000); }
      else { mostrarErro(d.msg || 'Erro ao iniciar'); }
    })
    .catch(() => mostrarErro('Erro de conexão'));
}

function verificarStatus() {
  _elapsed += 3;
  const pct = Math.min(95, Math.round(_elapsed / 120 * 100));
  document.getElementById('pfill').style.width = pct + '%';
  fetch('/admin/fotos/status')
    .then(r => r.json())
    .then(d => {
      if (d.status === 'done') {
        clearInterval(_poll);
        document.getElementById('pfill').style.width = '100%';
        const s = d.stats || {};
        const linkFaltantes = s.sem_foto > 0
          ? '<br><a class="dl-link" href="/admin/fotos/faltantes.xlsx" target="_blank">⬇ Baixar lista de itens sem foto (' + s.sem_foto + ')</a>'
          : '';
        document.getElementById('status-box').innerHTML =
          '<div class="msg ok">✓ Fotos atualizadas com sucesso!<br>' +
          'Total: <strong>' + s.total + '</strong> cores · ' +
          'Completas: <strong>' + s.completas + '</strong> · ' +
          'Parciais: <strong>' + s.parciais + '</strong> · ' +
          'Sem foto: <strong>' + s.sem_foto + '</strong> · ' +
          'Cobertura: <strong>' + s.cobertura_pct + '%</strong>' +
          linkFaltantes + '</div>';
        document.getElementById('btn').disabled = false;
        document.getElementById('pbar').style.display = 'none';
      } else if (d.status === 'error') {
        clearInterval(_poll);
        mostrarErro(d.msg || 'Erro desconhecido');
      } else {
        document.getElementById('status-box').innerHTML =
          '<div class="msg run"><span class="spinner"></span>Buscando fotos no Inside Boaonda… ' + _elapsed + 's</div>';
      }
    })
    .catch(() => {});
}

function mostrarErro(msg) {
  document.getElementById('status-box').innerHTML =
    '<div class="msg err">✗ ' + msg + '</div>';
  document.getElementById('btn').disabled = false;
  document.getElementById('pbar').style.display = 'none';
}

// Ao carregar a página, mostra o resultado da última atualização (mesmo que
// tenha sido rodada numa visita anterior) — inclui o link de download da
// lista de itens sem foto, sem precisar rodar a busca de novo só para vê-lo.
fetch('/admin/fotos/status')
  .then(r => r.json())
  .then(d => {
    if (d.status === 'running') {
      document.getElementById('btn').disabled = true;
      document.getElementById('pbar').style.display = 'block';
      _elapsed = 0; _poll = setInterval(verificarStatus, 3000);
      verificarStatus();
    } else if (d.status === 'done') {
      const s = d.stats || {};
      const linkFaltantes = s.sem_foto > 0
        ? '<br><a class="dl-link" href="/admin/fotos/faltantes.xlsx" target="_blank">⬇ Baixar lista de itens sem foto (' + s.sem_foto + ')</a>'
        : '';
      document.getElementById('status-box').innerHTML =
        '<div class="msg ok">Última atualização: <strong>' + (d.ts || '–') + '</strong><br>' +
        'Total: <strong>' + s.total + '</strong> cores · ' +
        'Completas: <strong>' + s.completas + '</strong> · ' +
        'Parciais: <strong>' + s.parciais + '</strong> · ' +
        'Sem foto: <strong>' + s.sem_foto + '</strong> · ' +
        'Cobertura: <strong>' + s.cobertura_pct + '%</strong>' +
        linkFaltantes + '</div>';
    }
  })
  .catch(() => {});
</script>
</body>
</html>'''


# Estado do job de fotos — persistido em arquivo para sobreviver entre workers
FOTOS_JOB_FILE = DATA_DIR / 'fotos_job.json'

def _ler_job():
    try:
        return json.loads(FOTOS_JOB_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {'status': 'idle'}

def _salvar_job(dados):
    try:
        FOTOS_JOB_FILE.write_text(json.dumps(dados, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass

def _rodar_fotos_bg():
    try:
        import gerar_dados_fotos
        stats = gerar_dados_fotos.gerar(
            estoque_path=str(DATA_DIR / 'dados_estoque.json'),
            fotos_out=str(DATA_DIR / 'dados_fotos.json'),
        )
        if DATA_DIR != FRONTEND_DIR:
            shutil.copy(DATA_DIR / 'dados_fotos.json', FRONTEND_DIR / 'dados_fotos.json')
        _salvar_job({'status': 'done', 'stats': stats,
                     'ts': datetime.now().strftime('%d/%m/%Y %H:%M')})
    except Exception as ex:
        traceback.print_exc()
        _salvar_job({'status': 'error', 'msg': str(ex)})


@app.route('/admin/fotos', methods=['GET'])
def admin_fotos():
    return render_template_string(_FOTOS_HTML)


@app.route('/admin/fotos/start', methods=['POST'])
def admin_fotos_start():
    job = _ler_job()
    if job.get('status') == 'running':
        return jsonify({'ok': False, 'msg': 'Já existe um processo em andamento.'})
    _salvar_job({'status': 'running', 'started': datetime.now().strftime('%d/%m/%Y %H:%M')})
    t = threading.Thread(target=_rodar_fotos_bg, daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/admin/fotos/status')
def admin_fotos_status():
    return jsonify(_ler_job())


@app.route('/admin/fotos/faltantes.xlsx')
def admin_fotos_faltantes():
    """Exporta em Excel a lista de referência/linha/cor que ficaram SEM
    nenhuma foto na última atualização (gerado por gerar_dados_fotos.gerar,
    arquivo dados_fotos_faltantes.json)."""
    arq = DATA_DIR / 'dados_fotos_faltantes.json'
    if not arq.exists():
        return jsonify({'erro': 'Ainda não há uma atualização de fotos processada. '
                                 'Rode "Buscar fotos e atualizar catálogo" primeiro.'}), 404
    try:
        with open(arq, encoding='utf-8') as f_:
            dados = json.load(f_)
        itens = dados.get('itens', [])
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = 'Sem foto'
        ws.append(['Referência', 'Linha', 'Cor', 'Prefixo esperado no Inside'])
        for it in itens:
            ws.append([it.get('referencia', ''), it.get('linha', ''),
                       it.get('cor', ''), it.get('prefixo_esperado', '')])
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        ts = (dados.get('gerado_em') or '').replace('/', '-').replace(' ', '_').replace(':', 'h')
        return send_file(
            bio, as_attachment=True,
            download_name=f'fotos_sem_foto_{ts or "atual"}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': f'Falha ao gerar a lista: {ex}'}), 500


@app.route('/api/foto-proxy')
def foto_proxy():
    """Proxy server-side para imagens de produto — resolve CORS no PDF export."""
    url = request.args.get('url', '').strip()
    if not url or not url.startswith('https://'):
        return '', 400
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        if 'boaonda.com.br' not in domain:
            return '', 403
    except Exception:
        return '', 400
    try:
        import urllib.request as ureq
        req = ureq.Request(url, headers={'User-Agent': 'Mozilla/5.0 Boaonda-Catalogo/1.0'})
        with ureq.urlopen(req, timeout=8) as r:
            data = r.read()
            ct = r.headers.get('Content-Type', 'image/jpeg').split(';')[0]
        from flask import Response
        return Response(data, mimetype=ct, headers={
            'Cache-Control': 'public, max-age=86400',
        })
    except Exception:
        traceback.print_exc()
        return '', 502


@app.route('/admin/home')
def admin_home():
    return send_from_directory(FRONTEND_DIR, 'admin_home.html')


@app.route('/promo-imagem')
def promo_imagem():
    """Rota de compatibilidade — serve a imagem do slot 0."""
    return promo_imagem_idx(0)


@app.route('/promo-imagem/<int:idx>')
def promo_imagem_idx(idx):
    """Serve a imagem de promoção pelo índice (0-3) — pública."""
    if idx < 0 or idx > 3:
        return '', 404
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        p = DATA_DIR / f'promo_imagem_{idx}{ext}'
        if p.exists():
            return send_from_directory(DATA_DIR, f'promo_imagem_{idx}{ext}', max_age=0)
    # backward compat: slot 0 também aceita o nome antigo sem índice
    if idx == 0:
        for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
            p = DATA_DIR / f'promo_imagem{ext}'
            if p.exists():
                return send_from_directory(DATA_DIR, f'promo_imagem{ext}', max_age=0)
    return '', 404


@app.route('/admin/home/upload-imagem', methods=['POST'])
def admin_home_upload_imagem():
    idx = int(request.form.get('idx', 0))
    if idx < 0 or idx > 3:
        return jsonify({'status': 'erro', 'mensagem': 'Índice inválido.'}), 400
    f = request.files.get('imagem')
    if not f or not f.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        return jsonify({'status': 'erro', 'mensagem': 'Formato não suportado. Use JPG, PNG ou WEBP.'}), 400
    for old_ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        old = DATA_DIR / f'promo_imagem_{idx}{old_ext}'
        if old.exists():
            old.unlink()
    saved_name = f'promo_imagem_{idx}{ext}'
    save_path = DATA_DIR / saved_name
    f.save(str(save_path))
    if DATA_DIR != FRONTEND_DIR:
        shutil.copy(save_path, FRONTEND_DIR / saved_name)
    return jsonify({'status': 'ok', 'url': f'/promo-imagem/{idx}'})


@app.route('/admin/home/remove-imagem', methods=['POST'])
def admin_home_remove_imagem():
    data = request.get_json(silent=True) or {}
    idx = int(data.get('idx', -1))
    if idx < 0 or idx > 3:
        return jsonify({'status': 'erro', 'mensagem': 'Índice inválido.'}), 400
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        p = DATA_DIR / f'promo_imagem_{idx}{ext}'
        if p.exists():
            p.unlink()
            if DATA_DIR != FRONTEND_DIR:
                fp = FRONTEND_DIR / f'promo_imagem_{idx}{ext}'
                if fp.exists():
                    fp.unlink()
    return jsonify({'status': 'ok'})


@app.route('/admin/home/save', methods=['POST'])
def admin_home_save():
    dados = request.get_json(silent=True)
    if not dados or not isinstance(dados, dict):
        return jsonify({'status': 'erro', 'mensagem': 'Payload inválido.'}), 400
    dados['gerado_em'] = datetime.now().strftime('%d/%m/%Y %H:%M')
    out = DATA_DIR / 'dados_home.json'
    try:
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        if DATA_DIR != FRONTEND_DIR:
            shutil.copy(out, FRONTEND_DIR / 'dados_home.json')
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'status': 'erro', 'mensagem': str(ex)}), 500
    return jsonify({'status': 'ok'})


@app.route('/admin/recarregar', methods=['GET', 'POST'])
def recarregar():
    if DATA_DIR == FRONTEND_DIR:
        return render_template_string(_RECARREGAR_HTML,
            message='Ambiente local: os dados já são lidos direto de frontend/, nada a recarregar.',
            ok=True, disabled=True)

    message, ok = None, True
    if request.method == 'POST':
        copiados = []
        for fname in DATA_FILES:
            src = FRONTEND_DIR / fname
            if src.exists():
                shutil.copy(src, DATA_DIR / fname)
                copiados.append(fname)
        message = f'{len(copiados)} arquivo(s) recarregados do último deploy para o volume.'

    return render_template_string(_RECARREGAR_HTML, message=message, ok=ok, disabled=False)


@app.route('/api/capacidade/exportar')
def capacidade_exportar():
    bloqueio = _exige_modulo('capacidade')
    if bloqueio:
        return bloqueio
    from processador_capacidade import exportar_capacidade_excel
    from flask import send_file
    dados_path = DATA_DIR / 'dados_capacidade.json'
    if not dados_path.exists():
        return jsonify({'erro': 'dados_capacidade.json não encontrado'}), 404
    try:
        buf = exportar_capacidade_excel(str(dados_path))
        return send_file(
            buf,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='Boaonda_Capacidade_Fabril.xlsx',
        )
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': str(ex)}), 500


@app.route('/api/capacidade/importar', methods=['POST'])
def capacidade_importar():
    from processador_capacidade import importar_capacidade_excel
    f = request.files.get('arquivo')
    if not f or not f.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400
    path_xlsx = UPLOADS_DIR / 'capacidade_import.xlsx'
    f.save(str(path_xlsx))
    try:
        resultado = importar_capacidade_excel(
            str(path_xlsx),
            str(DATA_DIR / 'dados_capacidade.json'),
            str(DATA_DIR),
        )
        # Capacidade mudou — recalcula ocupação/eficiência contra a
        # programação atual (não bloqueia a importação se ainda não houver
        # dados_programacao_detalhe.json).
        if resultado.get('status') == 'ok':
            try:
                import calculo_ocupacao_semanal
                calculo_ocupacao_semanal.gerar(diretorio=str(DATA_DIR))
            except Exception:
                traceback.print_exc()
        return jsonify(resultado)
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'status': 'erro', 'mensagem': str(ex)}), 500
    finally:
        if path_xlsx.exists():
            path_xlsx.unlink()


# Metas comerciais — registro formal via planilha em grade (admin, dentro das
# Configurações). Ambos os endpoints são admin-only (ADMIN_ONLY_EXATOS).
@app.route('/api/metas/exportar')
def metas_exportar():
    from processador_metas import exportar_metas_grade
    from flask import send_file
    carteira_path = DATA_DIR / 'dados_vendas_carteira.json'
    if not carteira_path.exists():
        return jsonify({'erro': 'dados_vendas_carteira.json não encontrado'}), 404
    try:
        buf = exportar_metas_grade(str(DATA_DIR / 'dados_metas.json'), str(carteira_path))
        return send_file(
            buf,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='Boaonda_Metas.xlsx',
        )
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': str(ex)}), 500


@app.route('/api/metas/importar', methods=['POST'])
def metas_importar():
    from processador_metas import importar_metas_grade
    f = request.files.get('arquivo')
    if not f or not f.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400
    path_xlsx = UPLOADS_DIR / 'metas_import.xlsx'
    f.save(str(path_xlsx))
    try:
        resultado = importar_metas_grade(
            str(path_xlsx),
            str(DATA_DIR / 'dados_metas.json'),
            str(DATA_DIR),
        )
        return jsonify(resultado)
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'status': 'erro', 'mensagem': str(ex)}), 500
    finally:
        if path_xlsx.exists():
            path_xlsx.unlink()


@app.route('/admin/metas')
def admin_metas():
    return render_template_string(_METAS_ADMIN_HTML)


@app.route('/admin/diag')
def admin_diag():
    """Diagnóstico da fonte 3YS (Vendas/Carteira/Programação). Mostra quantas
    linhas a fonte (MySQL) devolve e se os campos-chave vêm preenchidos —
    para investigar painéis zerados sem ter que olhar logs do Railway."""
    try:
        return app.response_class(
            json.dumps(processador.diagnostico_3ys(), ensure_ascii=False, indent=2,
                       default=str),
            mimetype='application/json',
        )
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': str(ex)}), 500


# Detalhe de faturamento por canal/mês — exporta um Excel com as linhas que
# compõem o faturamento daquele canal no mês (para conferência). Lê o arquivo
# de detalhe gerado pelo processador (dados_faturamento_det_AAAAMM.json).
FAT_DET_COLS = [
    ('pedido', 'Pedido'), ('cliente', 'Cliente'), ('ref', 'Referência'),
    ('descr', 'Descrição'), ('especie', 'Espécie'), ('tipo', 'Tipo'),
    ('cfop', 'CFOP'), ('conta', 'Conta contábil'), ('grupo', 'Grupo'),
    ('qtd', 'Qtd (pares/kg)'), ('vlr_liq', 'Valor líquido'),
    ('vlr_bruto', 'Valor bruto'), ('dt_ent', 'Dt entrada'),
    ('dt_fat', 'Dt faturamento'), ('dt_plano', 'Dt plano'),
    ('mes_ref', 'Mês ref'), ('status', 'Status'), ('nao_soma', 'Não soma'),
]


@app.route('/api/faturamento/detalhe')
def api_faturamento_detalhe():
    bloqueio = _exige_modulo('faturamento')
    if bloqueio:
        return bloqueio
    canal = (request.args.get('canal') or '').upper()
    mes   = (request.args.get('mes') or '').strip()
    if canal not in ('MI', 'ME', 'EC', 'EVA') or not (mes.isdigit() and len(mes) == 6):
        return jsonify({'erro': 'Parâmetros inválidos (canal MI/ME/EC/EVA e mes AAAAMM).'}), 400
    arq = DATA_DIR / f'dados_faturamento_det_{mes}.json'
    if not arq.exists():
        return jsonify({'erro': 'Detalhe indisponível para este mês. Reprocesse os dados '
                                '(a exportação é gerada no processamento do faturamento).'}), 404
    try:
        with open(arq, encoding='utf-8') as f_:
            dados = json.load(f_)
        linhas = dados.get(canal, [])
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = f'{canal} {mes}'
        ws.append([c[1] for c in FAT_DET_COLS])
        for r in linhas:
            ws.append([r.get(c[0], '') for c in FAT_DET_COLS])
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        return send_file(
            bio, as_attachment=True,
            download_name=f'faturamento_{canal}_{mes}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': f'Falha ao gerar o detalhe: {ex}'}), 500


# ─────────────────────────────────────────────
#  INTELIGÊNCIA — chat com IA sobre os dados do portal (Fase A)
# ─────────────────────────────────────────────
# Modelo e preços (Sonnet 4.6: US$ 3 / 1M entrada, US$ 15 / 1M saída).
IA_MODELO        = 'claude-sonnet-4-6'
IA_PRECO_IN_MTOK = 3.0
IA_PRECO_OUT_MTOK = 15.0

# Nome do contexto → arquivo JSON em DATA_DIR. 'estoque' é tratado à parte
# (resumido), pois o JSON completo tem ~240k chars.
IA_CONTEXTO_ARQUIVOS = {
    'programacao':  'dados_programacao.json',
    'ocupacao':     'dados_ocupacao_semanal.json',
    'vendas':       'dados_vendas.json',
    'vendas_eva':   'dados_vendas_eva.json',
    'faturamento':  'dados_faturamento.json',
    'carteira':     'dados_carteira.json',
    'refs':         'dados_refs_tabela.json',
    'capacidade':   'dados_capacidade.json',
    'portal':       'dados_portal.json',
}

IA_SYSTEM_BASE = """Você é o assistente de inteligência do Boaonda Intelligence, \
sistema de gestão da Boaonda Calçados (Mould Indústria de Matrizes Ltda, Sapiranga/RS).

Você tem acesso aos dados reais do portal abaixo e deve responder perguntas do \
gestor de forma direta, analítica e em português brasileiro.

REGRAS:
- Responda sempre baseado nos dados fornecidos, nunca invente números.
- Seja direto: comece com a resposta, depois explique o raciocínio.
- Use linguagem de negócios (pares, semana, meta, ocupação, gargalo).
- Quando identificar um problema ou oportunidade, sinalize claramente.
- Formato: texto corrido, sem markdown excessivo, máximo 4 parágrafos.
- Se os dados não forem suficientes para responder, diga claramente.

CONTEXTO DO NEGÓCIO:
- Meta semanal de produção: 30.000 pares.
- Canais de venda: MI (Mercado Interno), ME (Mercado Externo), EC (E-commerce).
- Espécies MI: Programação (cod 1), Pronta Entrega (cod 22), Venda Mista (cod 31).
- Teto do atelier: 29.000 pares/semana convencional + 6.000 montado.
- Gargalo = indicador com maior % de ocupação entre todos os tetos."""


IA_LIMITE_PADRAO_USD = 50.0   # teto mensal padrão se não configurado
IA_USO_ARQUIVO       = 'ia_uso.json'   # acumulado de gasto, no volume (DATA_DIR)


def _ia_carregar_json(nome):
    try:
        with open(DATA_DIR / nome, encoding='utf-8') as f_:
            return json.load(f_)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _ia_limite_mensal():
    cfg = _ia_carregar_json('config_producao.json') or {}
    try:
        return float(cfg.get('ia_limite_mensal_usd', IA_LIMITE_PADRAO_USD))
    except (TypeError, ValueError):
        return IA_LIMITE_PADRAO_USD


def _ia_uso_carregar():
    return _ia_carregar_json(IA_USO_ARQUIVO) or {}


def _ia_uso_resumo():
    """Gasto e nº de perguntas do mês e do dia atuais + limite configurado."""
    dados = _ia_uso_carregar()
    mes = datetime.now().strftime('%Y-%m')
    dia = datetime.now().strftime('%Y-%m-%d')
    m = dados.get(mes, {})
    d = (m.get('dias', {}) or {}).get(dia, {})
    return {
        'mes': mes,
        'custo_mes': round(m.get('custo', 0.0), 4),
        'perguntas_mes': int(m.get('perguntas', 0)),
        'custo_hoje': round(d.get('custo', 0.0), 4),
        'perguntas_hoje': int(d.get('perguntas', 0)),
        'limite_mensal_usd': _ia_limite_mensal(),
    }


def _ia_uso_registrar(custo):
    """Soma o custo de uma pergunta ao acumulado do mês/dia (no volume)."""
    dados = _ia_uso_carregar()
    mes = datetime.now().strftime('%Y-%m')
    dia = datetime.now().strftime('%Y-%m-%d')
    m = dados.setdefault(mes, {'custo': 0.0, 'perguntas': 0, 'dias': {}})
    m['custo'] = round(m.get('custo', 0.0) + custo, 6)
    m['perguntas'] = int(m.get('perguntas', 0)) + 1
    d = m.setdefault('dias', {}).setdefault(dia, {'custo': 0.0, 'perguntas': 0})
    d['custo'] = round(d.get('custo', 0.0) + custo, 6)
    d['perguntas'] = int(d.get('perguntas', 0)) + 1
    try:
        with open(DATA_DIR / IA_USO_ARQUIVO, 'w', encoding='utf-8') as f_:
            json.dump(dados, f_, ensure_ascii=False)
    except Exception:
        traceback.print_exc()


def resumir_estoque():
    """Versão compacta do estoque para o contexto da IA: totais + top 15 refs
    por estoque livre + resumo de grades completas. Evita enviar os ~240k chars
    do dados_estoque.json em toda pergunta."""
    d = _ia_carregar_json('dados_estoque.json')
    if not d:
        return None
    refs = d.get('refs', {})
    top = sorted(refs.items(), key=lambda kv: -(kv[1].get('livre', 0)))[:15]
    return {
        'gerado_em': d.get('gerado_em'),
        'totais': d.get('totais', {}),
        'top15_refs_por_livre': [
            {'ref': k, 'livre': v.get('livre', 0), 'fisico': v.get('fisico', 0),
             'reservas': v.get('reservas', 0)}
            for k, v in top
        ],
        'grades_completas_disponiveis': d.get('grades_global', []),
    }


def _mes_de_data(dt):
    """'01/06/2026' -> '2026-06' (para agrupar pedidos por mês)."""
    if not dt or '/' not in dt:
        return ''
    p = dt.split('/')
    return f"{p[2]}-{p[1]}" if len(p) == 3 else ''


def resumir_carteira():
    """Carteira compacta: totais + canais + etapas + entrada/entrega por mês +
    os maiores pedidos (globais e o maior de cada mês de entrega). Evita enviar
    a lista inteira de pedidos, que cresce com o volume."""
    d = _ia_carregar_json('dados_carteira.json')
    if not d:
        return None
    peds = d.get('pedidos', []) or []
    top = sorted(peds, key=lambda x: (x.get('pares') or 0), reverse=True)[:50]
    maior_por_mes = {}
    for p in peds:
        mes = _mes_de_data(p.get('dt_faturam'))
        if not mes:
            continue
        atual = maior_por_mes.get(mes)
        if not atual or (p.get('pares') or 0) > (atual.get('pares') or 0):
            maior_por_mes[mes] = p
    return {
        'gerado_em': d.get('gerado_em'),
        'total_pedidos': d.get('total_pedidos'),
        'total_pares': d.get('total_pares'),
        'canais': d.get('canais', {}),
        'etapas': d.get('etapas', []),
        'mes_entrada': d.get('mes_entrada', {}),
        'mes_entrega': d.get('mes_entrega', {}),
        'obs': (f"Carteira tem {len(peds)} pedidos. 'top_pedidos' = os 50 maiores "
                "por pares; 'maior_pedido_por_mes_entrega' = o maior pedido de cada "
                "mês (pelo mês de dt_faturam/entrega prevista)."),
        'maior_pedido_por_mes_entrega': maior_por_mes,
        'top_pedidos': top,
    }


def _top_refs(refs, n):
    """{ref: qtd, ...} -> as n refs de maior qtd."""
    if isinstance(refs, dict):
        itens = sorted(refs.items(), key=lambda kv: (kv[1] or 0), reverse=True)[:n]
        return dict(itens)
    return refs


def resumir_refs():
    """Refs por período compactas: top refs de cada mês + das semanas recentes.
    O JSON completo lista todas as refs de todas as semanas (cresce muito)."""
    d = _ia_carregar_json('dados_refs_tabela.json')
    if not d:
        return None
    meses = {k: {'label': v.get('label'), 'top_refs': _top_refs(v.get('refs', {}), 25)}
             for k, v in (d.get('meses') or {}).items()}
    sem = d.get('semanas') or {}
    ult = sorted(sem)[-8:]
    semanas = {k: {'label': sem[k].get('label'),
                   'top_refs': _top_refs(sem[k].get('refs', {}), 20)}
               for k in ult}
    return {
        'obs': "Top refs por período (todos os meses + as 8 semanas mais recentes).",
        'meses': meses,
        'semanas_recentes': semanas,
    }


def resumir_programacao():
    """Programação compacta: meses (todos) + as 16 semanas mais recentes +
    refs_top15. Descarta o histórico antigo de semanas (o JSON tem ~76)."""
    d = _ia_carregar_json('dados_programacao.json')
    if not d:
        return None
    sem = d.get('semanas') or {}
    ult = sorted(sem)[-16:]
    semanas = {k: sem[k] for k in ult}
    return {
        'gerado_em': d.get('gerado_em'),
        'meses': d.get('meses', {}),
        'refs_top15': d.get('refs_top15', []),
        'obs': f"Mostrando as {len(semanas)} semanas mais recentes de {len(sem)} totais.",
        'semanas_recentes': semanas,
    }


# Resumidores por contexto — reduzem o JSON cru a agregados + top-N antes de
# enviar à IA. Contextos fora deste dict vão crus (limitados pelos tetos abaixo).
IA_RESUMIDORES = {
    'estoque':     resumir_estoque,
    'carteira':    resumir_carteira,
    'refs':        resumir_refs,
    'programacao': resumir_programacao,
}


# Tetos de tamanho do contexto enviado à IA. A janela do modelo é 1M tokens
# (~4M chars); mantemos MUITA folga para custo/latência e para nunca estourar
# o limite conforme os dados crescem. ~4 chars por token.
IA_CTX_MAX_CHARS_TOTAL   = 520_000   # ~130k tokens no total (todos os contextos)
IA_CTX_MAX_CHARS_POR_CTX = 60_000    # ~15k tokens por contexto — cabe os 8 juntos


def montar_contexto(contextos_list):
    """Lê os JSONs solicitados de DATA_DIR e devolve (texto_para_prompt,
    lista_de_contextos_efetivamente_usados).

    Aplica um teto de tamanho: cada contexto é truncado a
    IA_CTX_MAX_CHARS_POR_CTX e o total a IA_CTX_MAX_CHARS_TOTAL, para a
    requisição nunca exceder a janela do modelo (causa do erro 400
    'prompt is too long' quando os dados do 3YS crescem)."""
    if not contextos_list:
        contextos_list = ['portal']
    blocos, usados = [], []
    total = 0
    for ctx in contextos_list:
        if total >= IA_CTX_MAX_CHARS_TOTAL:
            break
        resumidor = IA_RESUMIDORES.get(ctx)
        if resumidor:
            dados = resumidor()
        else:
            arq = IA_CONTEXTO_ARQUIVOS.get(ctx)
            dados = _ia_carregar_json(arq) if arq else None
        if dados is None:
            continue
        corpo = json.dumps(dados, ensure_ascii=False, separators=(',', ':'))
        nota = ''
        # Teto por contexto
        if len(corpo) > IA_CTX_MAX_CHARS_POR_CTX:
            corpo = corpo[:IA_CTX_MAX_CHARS_POR_CTX]
            nota = ("\n[... dados truncados por tamanho — este contexto é maior "
                    "que o limite; os primeiros registros estão acima ...]")
        # Teto do total acumulado
        restante = IA_CTX_MAX_CHARS_TOTAL - total
        if len(corpo) > restante:
            corpo = corpo[:restante]
            nota = ("\n[... dados truncados por tamanho — limite total de "
                    "contexto atingido ...]")
        bloco = f"### {ctx.upper()}\n{corpo}{nota}"
        blocos.append(bloco)
        usados.append(ctx)
        total += len(bloco)
    if not usados:  # nada carregou — cai para o portal (resumo da home)
        dados = _ia_carregar_json('dados_portal.json')
        if dados is not None:
            blocos.append("### PORTAL\n" + json.dumps(dados, ensure_ascii=False))
            usados.append('portal')
    return "\n\n".join(blocos), usados


@app.route('/api/inteligencia', methods=['POST'])
def api_inteligencia():
    payload   = request.get_json(silent=True) or {}
    pergunta  = (payload.get('pergunta') or '').strip()
    contextos = payload.get('contextos') or []
    historico = payload.get('historico') or []   # [{role, content}, ...]

    # Filtra os contextos pelos módulos que o usuário tem acesso — a IA não
    # pode vazar dado de um módulo restrito só porque o cliente pediu no
    # payload. Contextos sem módulo associado (ex.: 'portal', o resumo da
    # home) continuam liberados para qualquer usuário logado.
    usuario_ia = _achar_usuario(session.get('username'))
    modulos_permitidos = _modulos_do_usuario(usuario_ia)
    contextos = [c for c in contextos
                 if _IA_CONTEXTO_PARA_MODULO.get(c) is None
                 or _IA_CONTEXTO_PARA_MODULO.get(c) in modulos_permitidos]

    if not pergunta:
        return jsonify({'erro': 'Pergunta vazia.'}), 400
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return jsonify({'erro': 'ANTHROPIC_API_KEY não está configurada no servidor. '
                                'Configure a variável no Railway para habilitar a Inteligência.'}), 503

    # Limite mensal de gasto — bloqueia novas perguntas quando atingido.
    limite = _ia_limite_mensal()
    resumo = _ia_uso_resumo()
    if limite > 0 and resumo['custo_mes'] >= limite:
        return jsonify({
            'erro': f"Limite mensal de gasto da IA atingido "
                    f"(US$ {resumo['custo_mes']:.2f} de US$ {limite:.2f}). "
                    f"Ajuste o teto em Configurações para continuar.",
            'limite_atingido': True,
            'uso': resumo,
        }), 200

    try:
        import anthropic
    except ImportError:
        return jsonify({'erro': "Biblioteca 'anthropic' não instalada no servidor."}), 500

    contexto_txt, contextos_usados = montar_contexto(contextos)
    system = IA_SYSTEM_BASE + "\n\nDADOS DO PORTAL:\n" + (contexto_txt or '(sem dados carregados)')

    # Histórico: últimas 6 trocas (3 perguntas + 3 respostas) já vem limitado do
    # frontend; reforçamos aqui. Só pares user/assistant de texto.
    mensagens = []
    for m in historico[-6:]:
        role = m.get('role')
        cont = (m.get('content') or '').strip()
        if role in ('user', 'assistant') and cont:
            mensagens.append({'role': role, 'content': cont})
    mensagens.append({'role': 'user', 'content': pergunta})

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=IA_MODELO,
            max_tokens=1500,
            thinking={'type': 'disabled'},   # Q&A direto — sem raciocínio estendido
            # Cache do bloco de dados: como o contexto é idêntico entre perguntas
            # da mesma sessão, a partir da 2ª pergunta ele é lido do cache
            # (muito mais barato e bem mais leve no limite de tokens/minuto).
            system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            messages=mensagens,
        )
    except anthropic.AuthenticationError:
        return jsonify({'erro': 'Chave da API Anthropic inválida.'}), 502
    except anthropic.RateLimitError:
        return jsonify({'erro': 'Limite de uso por minuto da conta Anthropic atingido '
                                '(o portal envia muitos dados por pergunta). Aguarde cerca '
                                'de 1 minuto e tente novamente. Se acontecer com frequência, '
                                'aumente o tier de rate limit no Console da Anthropic.'}), 429
    except anthropic.APIStatusError as ex:
        traceback.print_exc()
        # Expõe a mensagem real da API (o motivo do 400 fica no corpo da resposta,
        # não só no status). Sem isso, um 400 vira "tente novamente" sem diagnóstico.
        detalhe = ''
        try:
            corpo = ex.body
            if isinstance(corpo, dict):
                detalhe = (corpo.get('error') or {}).get('message') or ''
            detalhe = detalhe or getattr(ex, 'message', '') or str(ex)
        except Exception:
            detalhe = str(ex)
        print(f"  [IA] API {ex.status_code}: {detalhe}", flush=True)
        return jsonify({
            'erro': f'Erro da API Anthropic ({ex.status_code}): {detalhe[:400]}'
        }), 502
    except Exception as ex:
        traceback.print_exc()
        return jsonify({'erro': f'Falha ao consultar a IA: {ex}'}), 500

    if resp.stop_reason == 'refusal':
        return jsonify({'erro': 'A IA recusou responder a esta solicitação.'}), 200

    texto = next((b.text for b in resp.content if b.type == 'text'), '').strip()
    u = resp.usage
    tin_normal = u.input_tokens
    tin_cache_escrita = getattr(u, 'cache_creation_input_tokens', 0) or 0
    tin_cache_leitura = getattr(u, 'cache_read_input_tokens', 0) or 0
    tout = u.output_tokens
    # Preços de cache da Anthropic: escrita = 1,25x o preço normal de input,
    # leitura (cache hit) = 0,1x — é o que torna a 2ª+ pergunta da sessão
    # muito mais barata e mais leve no limite de tokens/minuto.
    custo = round(
        tin_normal * IA_PRECO_IN_MTOK / 1e6
        + tin_cache_escrita * (IA_PRECO_IN_MTOK * 1.25) / 1e6
        + tin_cache_leitura * (IA_PRECO_IN_MTOK * 0.1) / 1e6
        + tout * IA_PRECO_OUT_MTOK / 1e6,
        4,
    )
    tin = tin_normal + tin_cache_escrita + tin_cache_leitura
    _ia_uso_registrar(custo)

    return jsonify({
        'resposta': texto,
        'contextos_usados': contextos_usados,
        'tokens_usados': tin + tout,
        'custo_estimado_usd': custo,
        'uso': _ia_uso_resumo(),
    })


@app.route('/api/inteligencia/uso')
def api_inteligencia_uso():
    return jsonify(_ia_uso_resumo())


@app.route('/admin/env-check')
def admin_env_check():
    """Confirma quais variáveis de ambiente sensíveis o processo está enxergando
    (apenas presença/sufixo, nunca o valor). Para depurar a Inteligência sem
    precisar olhar logs do Railway."""
    def _mask(nome):
        v = os.environ.get(nome) or ''
        v = v.strip()
        if not v:
            return {'configurada': False}
        return {'configurada': True, 'tamanho': len(v),
                'comeca_com': v[:7], 'termina_com': v[-4:],
                'tem_espaco_nas_pontas': v != os.environ.get(nome, '')}
    return jsonify({
        'ANTHROPIC_API_KEY': _mask('ANTHROPIC_API_KEY'),
        'MYSQL_HOST': {'configurada': bool(os.environ.get('MYSQL_HOST'))},
        'DATA_DIR': os.environ.get('DATA_DIR') or '(default frontend/)',
    })
@app.route('/admin/db-tunnel-status')
def admin_db_tunnel_status():
    """Confirma se o túnel Cloudflare + conexão com o Supabase interno estão
    funcionando de dentro do Railway. Rota temporária, útil enquanto as
    tabelas dom_/fact_ ainda não estão prontas — depois que a migração para
    SQL estiver em uso de verdade, pode virar parte do fluxo normal ou ser
    removida."""
    try:
        import psycopg2
    except ImportError:
        return jsonify({'status': 'erro',
                         'detalhe': "Biblioteca 'psycopg2-binary' não instalada "
                                    "no servidor (adicionar ao requirements.txt)."}), 500

    try:
        conexao = psycopg2.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=os.environ.get('DB_PORT', '5432'),
            dbname=os.environ.get('DB_NAME', 'postgres'),
            user=os.environ.get('DB_USER'),
            password=os.environ.get('DB_PASSWORD'),
            connect_timeout=5,
        )
        cursor = conexao.cursor()
        cursor.execute("SELECT version();")
        versao = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public';
        """)
        total_tabelas = cursor.fetchone()[0]

        conexao.close()
        return jsonify({
            'status': 'ok',
            'postgres_version': versao,
            'tabelas_no_schema_public': total_tabelas,
        })
    except Exception as ex:
        return jsonify({'status': 'erro', 'detalhe': str(ex)}), 500


# ─────────────────────────────────────────────
#  GESTÃO DE REPRESENTANTES DO CATÁLOGO (admin-only) — vive no Supabase
#  (tabela catalogo_representantes), não no usuarios.json local, porque
#  precisa ser referenciável por FK a partir de catalogo_cadastros
#  (representante_id) pro painel do representante filtrar a carteira dele.
# ─────────────────────────────────────────────
_CATALOGO_REPS_HTML = '''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boaonda Intelligence — Representantes do Catálogo</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--coral:#ed6842;--verde:#6c9c37;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--line:#e2ddd8;--txt-s:#71706f;--txt-m:#9b9895}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',sans-serif}
body{background:var(--bg);color:var(--verde-dark);min-height:100vh;padding:32px}
.wrap{max-width:760px;margin:0 auto}
.brand{font-size:18px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:4px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:13px;margin-left:8px;letter-spacing:1px}
h1{font-size:16px;font-weight:700;margin:24px 0 8px}
h2{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin:28px 0 12px}
p.sub{font-size:12px;color:var(--txt-s);margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:16px}
.field{margin-bottom:16px}
label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin-bottom:8px}
input[type=text],input[type=email],input[type=password]{width:100%;background:#f3f0eb;border:1px solid var(--line);border-radius:8px;padding:10px 14px;color:var(--verde-dark);font-size:13px;outline:none;font-family:'Montserrat',sans-serif}
input:focus{border-color:var(--coral)}
.hint{font-size:11px;color:var(--txt-m);margin-top:4px}
.btn{background:var(--coral);color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:12px;font-weight:700;cursor:pointer}
.btn:hover{background:#dd7051}
.btn-sm{padding:7px 12px;font-size:11px}
.msg{border-radius:8px;padding:12px 16px;font-size:12px;margin-bottom:16px}
.msg.ok{background:rgba(108,156,55,.1);color:var(--verde);border:1px solid rgba(108,156,55,.25)}
.msg.err{background:rgba(239,68,68,.08);color:#c0392b;border:1px solid rgba(239,68,68,.25)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--txt-m);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);font-size:12px;vertical-align:middle}
tr:last-child td{border-bottom:none}
.tag-inativo{font-size:9px;font-weight:700;color:#c0392b;background:rgba(239,68,68,.08);border-radius:4px;padding:2px 6px;margin-left:6px}
.row-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.row-actions form{display:flex;gap:6px;align-items:center;margin:0}
.row-actions input[type=password]{width:130px;padding:6px 10px;font-size:11px}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Representantes do catálogo</h1>
  <p class="sub">Login exclusivo pro ambiente de pedidos de pronta entrega (catálogo público) — só existe se o admin criar aqui. Cada representante enxerga só os clientes que ele mesmo cadastrar.</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message }}</div>
  {% endif %}

  <div class="card">
    <table>
      <thead><tr><th>Nome</th><th>E-mail</th><th>Criado em</th><th>Ações</th></tr></thead>
      <tbody>
        {% for r in reps %}
        <tr>
          <td>{{ r.nome }}{% if not r.ativo %}<span class="tag-inativo">inativo</span>{% endif %}</td>
          <td>{{ r.email }}</td>
          <td>{{ (r.criado_em or '')[:16] }}</td>
          <td>
            <div class="row-actions">
              <form method="post" action="/admin/catalogo-representantes/senha">
                <input type="hidden" name="id" value="{{ r.id }}">
                <input type="password" name="nova_senha" placeholder="Nova senha" minlength="6" required>
                <button class="btn btn-sm" type="submit">Trocar senha</button>
              </form>
              <form method="post" action="/admin/catalogo-representantes/ativo">
                <input type="hidden" name="id" value="{{ r.id }}">
                <input type="hidden" name="ativo" value="{{ '0' if r.ativo else '1' }}">
                <button class="btn btn-sm" type="submit">{{ 'Desativar' if r.ativo else 'Reativar' }}</button>
              </form>
            </div>
          </td>
        </tr>
        {% else %}
        <tr><td colspan="4" style="color:var(--txt-m)">Nenhum representante cadastrado ainda.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <h2>Novo representante</h2>
  <form class="card" method="post" action="/admin/catalogo-representantes/criar">
    <div class="field">
      <label>Nome</label>
      <input type="text" name="nome" required>
    </div>
    <div class="field">
      <label>E-mail</label>
      <input type="email" name="email" autocomplete="off" required>
    </div>
    <div class="field">
      <label>Senha inicial</label>
      <input type="password" name="senha" minlength="6" required>
      <div class="hint">Mínimo 6 caracteres. O representante pode trocar depois de logar no catálogo.</div>
    </div>
    <button class="btn" type="submit">Cadastrar representante</button>
  </form>

  <a class="back" href="/" target="_top">← Voltar ao portal</a>
</div>
</body>
</html>'''


def _redirect_catalogo_reps(msg, ok=True):
    return redirect(url_for('admin_catalogo_representantes', msg=msg, ok='1' if ok else '0'))


@app.route('/admin/catalogo-representantes')
def admin_catalogo_representantes():
    msg = request.args.get('msg')
    ok = request.args.get('ok', '1') == '1'
    reps = []
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("SELECT id, nome, email, ativo, criado_em FROM catalogo_representantes ORDER BY nome")
        cols = [d[0] for d in cursor.description]
        reps = [dict(zip(cols, (_catalogo_valor_json_seguro(v) for v in row)))
                for row in cursor.fetchall()]
        conexao.close()
    except Exception as ex:
        msg = msg or f'Não foi possível carregar os representantes. ({ex})'
        ok = False
    return render_template_string(_CATALOGO_REPS_HTML, reps=reps, message=msg, ok=ok)


@app.route('/admin/catalogo-representantes/criar', methods=['POST'])
def admin_catalogo_representantes_criar():
    nome = request.form.get('nome', '').strip()
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')
    if not nome or not email or len(senha) < 6:
        return _redirect_catalogo_reps('Informe nome, e-mail e uma senha com pelo menos 6 caracteres.', ok=False)
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("""
            INSERT INTO catalogo_representantes (nome, email, senha_hash)
            VALUES (%s, %s, %s)
        """, (nome, email, generate_password_hash(senha)))
        conexao.commit()
        conexao.close()
        return _redirect_catalogo_reps(f'Representante "{nome}" cadastrado com sucesso.')
    except Exception as ex:
        if 'unique' in str(ex).lower() or 'duplicate' in str(ex).lower():
            return _redirect_catalogo_reps(f'Já existe um representante com o e-mail "{email}".', ok=False)
        return _redirect_catalogo_reps(f'Erro ao cadastrar: {ex}', ok=False)


@app.route('/admin/catalogo-representantes/senha', methods=['POST'])
def admin_catalogo_representantes_senha():
    rep_id = request.form.get('id', '')
    nova_senha = request.form.get('nova_senha', '')
    if len(nova_senha) < 6:
        return _redirect_catalogo_reps('A nova senha precisa ter pelo menos 6 caracteres.', ok=False)
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("UPDATE catalogo_representantes SET senha_hash = %s WHERE id = %s",
                       (generate_password_hash(nova_senha), rep_id))
        conexao.commit()
        conexao.close()
        return _redirect_catalogo_reps('Senha atualizada.')
    except Exception as ex:
        return _redirect_catalogo_reps(f'Erro ao atualizar senha: {ex}', ok=False)


@app.route('/admin/catalogo-representantes/ativo', methods=['POST'])
def admin_catalogo_representantes_ativo():
    rep_id = request.form.get('id', '')
    ativo = request.form.get('ativo') == '1'
    try:
        conexao = _conectar_catalogo_db()
        cursor = conexao.cursor()
        cursor.execute("UPDATE catalogo_representantes SET ativo = %s WHERE id = %s", (ativo, rep_id))
        conexao.commit()
        conexao.close()
        return _redirect_catalogo_reps('Representante ' + ('reativado.' if ativo else 'desativado.'))
    except Exception as ex:
        return _redirect_catalogo_reps(f'Erro ao atualizar: {ex}', ok=False)


_CATALOGO_DIAG_CSS = '''
:root{--coral:#ed6842;--verde-dark:#26361e;--bg:#f8f5f1;--card:#fff;--border:#e2ddd8;--txt-m:#71706f}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Montserrat',system-ui,sans-serif}
body{background:var(--bg);color:var(--verde-dark);padding:28px}
.brand{font-size:16px;font-weight:800;color:var(--coral);letter-spacing:2px;margin-bottom:2px}
.brand span{color:var(--verde-dark);font-weight:300;font-size:12px;margin-left:8px;letter-spacing:1px}
h1{font-size:15px;font-weight:700;margin:18px 0 4px}
p.sub{font-size:11.5px;color:var(--txt-m);margin-bottom:14px;line-height:1.5}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:22px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:11.5px;white-space:nowrap}
th{text-align:left;padding:6px 10px;color:var(--txt-m);font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border)}
td{padding:6px 10px;border-bottom:1px solid rgba(0,0,0,.04)}
tr:hover td{background:rgba(237,104,66,.04)}
.aviso{font-size:12px;color:var(--txt-m);font-style:italic;padding:6px 2px}
.ordem-nota{font-size:10px;color:var(--txt-m);margin-top:6px}
.back{display:inline-block;margin-top:6px;font-size:12px;color:var(--txt-m);text-decoration:none}
.back:hover{color:var(--coral)}
'''


def _catalogo_diag_colunas(cursor, tabela):
    cursor.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position
    """, (tabela,))
    return cursor.fetchall()


def _catalogo_diag_ordem(colunas):
    """Escolhe a coluna mais confiável para "mais recente primeiro". Prefere
    uma coluna de data/hora; só cai para 'id' se for tipo inteiro (serial) —
    id do tipo uuid não é cronológico, então nesse caso avisa que a ordem
    pode não refletir os registros mais recentes."""
    tipos = {nome: tipo for nome, tipo in colunas}
    for candidata in ('criado_em', 'created_at', 'data_criacao', 'inserted_at', 'data_cadastro', 'data'):
        if candidata in tipos:
            return candidata, True
    if tipos.get('id') in ('integer', 'bigint', 'smallint'):
        return 'id', True
    return 'id', False


def _catalogo_diag_tabela_html(conexao, titulo, sql_select, tabela_ordem, alvo_ordem, limit=20):
    """Executa um SELECT (com placeholder {ordem} pra coluna de ordenação) e
    devolve o HTML da seção — captura erro por tabela (schema diferente do
    esperado, tabela ainda não criada) sem derrubar o restante da página."""
    cursor = conexao.cursor()
    colunas_meta = _catalogo_diag_colunas(cursor, tabela_ordem)
    if not colunas_meta:
        return f'<div class="card"><h1>{titulo}</h1><p class="aviso">Tabela "{tabela_ordem}" não encontrada no schema public — confirme se a migração/criação da tabela já rodou no Supabase.</p></div>'
    ordem_col, confiavel = _catalogo_diag_ordem(colunas_meta)
    try:
        cursor.execute(sql_select.format(ordem=f'{alvo_ordem}.{ordem_col}'), (limit,))
        cols = [d[0] for d in cursor.description]
        linhas = cursor.fetchall()
    except Exception as ex:
        conexao.rollback()
        return f'<div class="card"><h1>{titulo}</h1><p class="aviso">Erro ao consultar: {html.escape(str(ex))}</p></div>'

    aviso_ordem = '' if confiavel else (
        '<p class="ordem-nota">⚠ Sem coluna de data e id não é sequencial — '
        'a ordem abaixo pode não refletir os registros mais recentes.</p>')
    if not linhas:
        return f'<div class="card"><h1>{titulo} (0)</h1><p class="aviso">Nenhum registro ainda.</p></div>'
    head = ''.join(f'<th>{html.escape(str(c))}</th>' for c in cols)
    body = ''.join(
        '<tr>' + ''.join(f'<td>{"" if v is None else html.escape(str(v))}</td>' for v in linha) + '</tr>'
        for linha in linhas
    )
    return (f'<div class="card"><h1>{titulo} ({len(linhas)})</h1>'
            f'{aviso_ordem}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


@app.route('/admin/catalogo-diag')
def admin_catalogo_diag():
    """Mostra os cadastros e pedidos mais recentes gravados pelo gate do
    catálogo (catalogo_cadastros/catalogo_pedidos/catalogo_pedidos_itens no
    Supabase) — confirma que um cadastro/pedido de teste foi gravado, sem
    precisar de acesso ao painel do Supabase. Rota temporária de apoio à
    validação do gate de CNPJ; remover ou restringir mais quando não for
    mais necessária."""
    try:
        import psycopg2
    except ImportError:
        return '<p>Biblioteca \'psycopg2-binary\' não instalada no servidor.</p>', 500

    try:
        conexao = _conectar_catalogo_db()
    except Exception as ex:
        return f'<p>Não foi possível conectar ao banco: {html.escape(str(ex))}</p>', 500

    secoes = []
    secoes.append(_catalogo_diag_tabela_html(
        conexao, 'Cadastros recentes',
        'SELECT * FROM catalogo_cadastros ORDER BY {ordem} DESC LIMIT %s',
        'catalogo_cadastros', 'catalogo_cadastros',
    ))
    secoes.append(_catalogo_diag_tabela_html(
        conexao, 'Pedidos recentes (com nome/CNPJ do cadastro)',
        '''SELECT p.*, c.nome AS cli_nome, c.cnpj AS cli_cnpj, c.empresa AS cli_empresa
           FROM catalogo_pedidos p
           LEFT JOIN catalogo_cadastros c ON c.id = p.cadastro_id
           ORDER BY {ordem} DESC LIMIT %s''',
        'catalogo_pedidos', 'p',
    ))
    secoes.append(_catalogo_diag_tabela_html(
        conexao, 'Itens de pedidos recentes',
        '''SELECT i.*, p.cadastro_id
           FROM catalogo_pedidos_itens i
           LEFT JOIN catalogo_pedidos p ON p.id = i.pedido_id
           ORDER BY {ordem} DESC LIMIT %s''',
        'catalogo_pedidos_itens', 'i',
    ))
    conexao.close()

    pagina = (f'<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"/>'
              f'<title>Diagnóstico — Catálogo</title><style>{_CATALOGO_DIAG_CSS}</style></head><body>'
              f'<div class="brand">BOAONDA <span>· Diagnóstico do gate do catálogo</span></div>'
              f'<p class="sub">Últimos registros gravados no Supabase pelo cadastro (CNPJ) e pelos pedidos do catálogo público. '
              f'Atualize a página (F5) para ver dados novos.</p>'
              + ''.join(secoes) +
              '<a class="back" href="/admin/configuracoes">← Voltar às Configurações</a>'
              '</body></html>')
    return pagina


@app.route('/version')
def version():
    import subprocess
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        commit = 'unknown'
    return jsonify({'commit': commit})


if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT', 8080)))
