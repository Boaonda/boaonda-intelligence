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
    'dados_refs_tabela.json', 'dados_vendas.json', 'dados_carteira.json',
    'boaonda_dados_completos.json', 'config_producao.json',
    'dados_capacidade.json', 'dados_ocupacao_semanal.json',
    'dados_faturamento.json',
)

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
    """Bloqueia todas as rotas exceto /login e /logout."""
    public = {'login', 'logout'}
    if request.endpoint not in public and not session.get('logged_in'):
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

    message, ok = None, True
    if request.method == 'POST':
        try:
            novo_prazo = int(request.form.get('prazo_producao_dias', ''))
            if novo_prazo < 1 or novo_prazo > 365:
                raise ValueError
        except ValueError:
            message, ok = 'Informe um número de dias entre 1 e 365.', False
        else:
            prazo = novo_prazo
            with open(config_path, 'w', encoding='utf-8') as f_:
                json.dump({'prazo_producao_dias': prazo}, f_, ensure_ascii=False, indent=2)
            message = f'Configuração salva: prazo produtivo de {prazo} dias.'

    return render_template_string(_CONFIG_HTML, message=message, ok=ok, prazo=prazo)


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
