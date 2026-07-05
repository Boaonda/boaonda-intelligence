from pathlib import Path
from datetime import datetime
import json
import os
import shutil
import traceback

from flask import (Flask, request, jsonify, send_from_directory,
                    session, redirect, url_for, render_template_string)

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
    'dados_carteira.json',
    'boaonda_dados_completos.json', 'config_producao.json',
    'dados_capacidade.json', 'dados_ocupacao_semanal.json',
    'dados_faturamento.json', 'dados_fotos.json', 'dados_home.json',
)

# Arquivos JSON servidos publicamente (sem autenticação) para o catálogo público.
DATA_FILES_PUBLICOS = {'dados_estoque.json', 'dados_fotos.json', 'dados_home.json'}

# Primeira execução com volume vazio: semeia com os JSONs versionados no repo
if DATA_DIR != FRONTEND_DIR:
    for _fname in DATA_FILES:
        _dst = DATA_DIR / _fname
        _src = FRONTEND_DIR / _fname
        if not _dst.exists() and _src.exists():
            shutil.copy(_src, _dst)

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-local-key-change-in-prod')
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024  # 250MB — 3YS.csv pode ter ~130MB

# ── Credenciais (definir via variáveis de ambiente no Railway) ─────────────────
AUTH_USERS = {
    os.environ.get('AUTH_USERNAME', 'admin'): os.environ.get('AUTH_PASSWORD', 'analytics2024'),
}

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


@app.before_request
def require_login():
    """Bloqueia todas as rotas exceto /login, /logout e o catálogo público."""
    public_endpoints = {'login', 'logout', 'catalogo', 'foto_proxy', 'promo_imagem'}
    if request.endpoint in public_endpoints:
        return None
    # JSONs necessários para o catálogo público não exigem autenticação
    if request.path.lstrip('/') in DATA_FILES_PUBLICOS:
        return None
    if not session.get('logged_in'):
        return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if AUTH_USERS.get(username) == password:
            session['logged_in'] = True
            session['username']  = username
            return redirect(url_for('index'))
        error = 'Usuário ou senha incorretos.'
    return render_template_string(_LOGIN_HTML, error=error)


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


@app.route('/catalogo')
def catalogo():
    """Catálogo público de produtos — sem autenticação."""
    return send_from_directory(FRONTEND_DIR, 'catalogo.html')


@app.route('/<path:filename>')
def serve_file(filename):
    # JSONs gerados pelo /upload vivem em DATA_DIR (volume persistente);
    # o restante (HTML/CSS/JS dos dashboards) vem do código versionado.
    if filename in DATA_FILES:
        return send_from_directory(DATA_DIR, filename)
    return send_from_directory(FRONTEND_DIR, filename)


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

  <a class="back" href="/">← Voltar ao portal</a>
  &nbsp;·&nbsp;
  <a class="back" href="/admin/fotos">Atualizar fotos do catálogo →</a>
  &nbsp;·&nbsp;
  <a class="back" href="/admin/home">Editar home do catálogo →</a>
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
        msg = (f"Dados atualizados em {resumo['gerado_em']}.<br>"
               f"Estoque livre: {t['livre']:,}".replace(',', '.') +
               f" | Vendas {resumo['mes_label']}: MI {cm.get('MI',0):,}".replace(',', '.'))
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

  <a class="back" href="/">← Voltar ao portal</a>
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

  <a class="back" href="/">← Voltar ao portal</a>
</div>
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
.stat{font-size:12px;color:var(--txt-s);margin-top:8px}
.stat strong{color:var(--verde-dark)}
.back{display:inline-block;margin-top:8px;font-size:12px;color:var(--txt-s);text-decoration:none}
.back:hover{color:var(--coral)}
.warn{background:rgba(237,104,66,.08);border:1px solid rgba(237,104,66,.2);border-radius:8px;
      padding:10px 14px;font-size:11px;color:var(--coral);margin-bottom:16px}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">BOAONDA <span>· Intelligence</span></div>
  <h1>Atualizar fotos do catálogo</h1>
  <p class="sub">Busca as imagens de produto no Inside Boaonda e gera o arquivo de fotos usado pelo catálogo público. Pode levar 2–3 minutos.</p>

  {% if message %}
  <div class="msg {{ 'ok' if ok else 'err' }}">{{ message|safe }}</div>
  {% endif %}

  <div class="warn">⏳ O processo pode demorar 2 a 3 minutos. Não feche a página enquanto estiver carregando.</div>

  <form class="card" method="post"
    onsubmit="document.getElementById('btn').textContent='Buscando fotos… aguarde';document.getElementById('btn').disabled=true">
    <button class="btn" id="btn" type="submit">Buscar fotos e atualizar catálogo</button>
  </form>

  <a class="back" href="/upload">← Voltar para Atualizar dados</a>
  &nbsp;·&nbsp;
  <a class="back" href="/catalogo" target="_blank">Ver catálogo público ↗</a>
</div>
</body>
</html>'''


@app.route('/admin/fotos', methods=['GET', 'POST'])
def admin_fotos():
    message, ok = None, True
    if request.method == 'POST':
        try:
            import gerar_dados_fotos
            stats = gerar_dados_fotos.gerar(
                estoque_path=str(DATA_DIR / 'dados_estoque.json'),
                fotos_out=str(DATA_DIR / 'dados_fotos.json'),
            )
            # Copia de volta para FRONTEND_DIR para persistir no repo
            if DATA_DIR != FRONTEND_DIR:
                shutil.copy(DATA_DIR / 'dados_fotos.json',
                            FRONTEND_DIR / 'dados_fotos.json')
            message = (
                f"Fotos atualizadas com sucesso!<br>"
                f"Total: {stats['total']} cores · "
                f"Completas: {stats['completas']} · "
                f"Parciais: {stats['parciais']} · "
                f"Sem foto: {stats['sem_foto']} · "
                f"Cobertura: {stats['cobertura_pct']}%"
            )
        except Exception as ex:
            traceback.print_exc()
            message = f'Erro ao buscar fotos: {ex}'
            ok = False
    return render_template_string(_FOTOS_HTML, message=message, ok=ok)


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
    """Serve a imagem da promoção — pública (sem autenticação)."""
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        p = DATA_DIR / f'promo_imagem{ext}'
        if p.exists():
            from flask import Response
            return send_from_directory(DATA_DIR, f'promo_imagem{ext}', max_age=0)
    return '', 404


@app.route('/admin/home/upload-imagem', methods=['POST'])
def admin_home_upload_imagem():
    f = request.files.get('imagem')
    if not f or not f.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        return jsonify({'status': 'erro', 'mensagem': 'Formato não suportado. Use JPG, PNG ou WEBP.'}), 400
    # Remove imagens anteriores de outros formatos
    for old_ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        old = DATA_DIR / f'promo_imagem{old_ext}'
        if old.exists():
            old.unlink()
    saved_name = f'promo_imagem{ext}'
    save_path = DATA_DIR / saved_name
    f.save(str(save_path))
    if DATA_DIR != FRONTEND_DIR:
        shutil.copy(save_path, FRONTEND_DIR / saved_name)
    return jsonify({'status': 'ok', 'url': '/promo-imagem'})


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


def montar_contexto(contextos_list):
    """Lê os JSONs solicitados de DATA_DIR e devolve (texto_para_prompt,
    lista_de_contextos_efetivamente_usados)."""
    if not contextos_list:
        contextos_list = ['portal']
    blocos, usados = [], []
    for ctx in contextos_list:
        if ctx == 'estoque':
            dados = resumir_estoque()
        else:
            arq = IA_CONTEXTO_ARQUIVOS.get(ctx)
            dados = _ia_carregar_json(arq) if arq else None
        if dados is None:
            continue
        blocos.append(f"### {ctx.upper()}\n" +
                      json.dumps(dados, ensure_ascii=False, separators=(',', ':')))
        usados.append(ctx)
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
        return jsonify({'erro': f'Erro da API Anthropic ({ex.status_code}). Tente novamente.'}), 502
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
