import os
import json
import re
import time
import pytz
import smtplib
import threading
import feedparser
import unicodedata
from urllib.parse import quote
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from flask import Flask, request, redirect, render_template_string, session
from werkzeug.security import generate_password_hash, check_password_hash
from google import genai
from google.genai import types

load_dotenv()

# --- CONFIGURAÇÕES ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
FLASK_SECRET = os.getenv("FLASK_SECRET", "ri_bot_secret_key_2026_super_safe")

PERFIS_FILE = "/app/data/perfis.json"

RSS_FEEDS_BASE = [
    {"nome": "BBC World News", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"nome": "Al Jazeera English", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"nome": "Google News - RI Global", "url": "https://news.google.com/rss/search?q=geopolitica+OR+relacoes+internacionais&hl=pt-BR&gl=BR&ceid=BR:pt-419"}
]

app = Flask(__name__)
app.secret_key = FLASK_SECRET

def normalizar_texto(texto):
    if not texto:
        return ""
    texto = unicodedata.normalize('NFD', texto)
    return ''.join(c for c in texto if unicodedata.category(c) != 'Mn').lower()

def obter_perfis():
    os.makedirs(os.path.dirname(PERFIS_FILE), exist_ok=True)
    if not os.path.exists(PERFIS_FILE):
        perfis = {}
        admin_email = (EMAIL_SENDER or "admin@admin.com").lower().strip()
        perfis[admin_email] = {
            "nome": "Administrador",
            "email": admin_email,
            "senha_hash": generate_password_hash("admin123"),
            "role": "admin",
            "termos": ["geopolitica", "brics"],
            "horarios": ["07:00", "13:00"],
            "ativo": True
        }
        salvar_perfis(perfis)
        return perfis

    try:
        with open(PERFIS_FILE, "r") as f:
            perfis = json.load(f)
            for email, p in perfis.items():
                if "senha_hash" not in p:
                    p["senha_hash"] = generate_password_hash("123456")
                if "role" not in p:
                    p["role"] = "admin" if email == EMAIL_SENDER else "user"
                if "horarios" not in p:
                    p["horarios"] = ["07:00"]
            return perfis
    except Exception:
        return {}

def salvar_perfis(perfis):
    os.makedirs(os.path.dirname(PERFIS_FILE), exist_ok=True)
    with open(PERFIS_FILE, "w") as f:
        json.dump(perfis, f, indent=2, ensure_ascii=False)

def obter_usuario_logado():
    email = session.get("user_email")
    if not email:
        return None
    return obter_perfis().get(email)

def coletar_noticias_rss_para_perfil(termos_perfil):
    noticias = []
    limite_tempo = datetime.now(pytz.utc) - timedelta(hours=24)
    termos_norm = [normalizar_texto(t) for t in termos_perfil if t.strip()]

    feeds_para_processar = list(RSS_FEEDS_BASE)

    if termos_perfil:
        query_str = " OR ".join([f'"{t}"' if " " in t else t for t in termos_perfil])
        query_encoded = quote(query_str)

        feeds_para_processar.append({
            "nome": "Google News (BR)",
            "url": f"https://news.google.com/rss/search?q={query_encoded}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        })
        feeds_para_processar.append({
            "nome": "Google News (Global)",
            "url": f"https://news.google.com/rss/search?q={query_encoded}&hl=en-US&gl=US&ceid=US:en"
        })

    links_vistos = set()

    for feed_info in feeds_para_processar:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries:
                link = entry.get("link", "#")
                if link in links_vistos:
                    continue

                pub_date = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=pytz.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_date = datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=pytz.utc)

                if pub_date is None or pub_date >= limite_tempo:
                    titulo = entry.get("title", "")
                    resumo = entry.get("summary", entry.get("description", ""))

                    if termos_norm:
                        texto_completo = normalizar_texto(f"{titulo} {resumo}")
                        if not any(termo in texto_completo for termo in termos_norm):
                            continue

                    links_vistos.add(link)
                    noticias.append({
                        "fonte": feed_info["nome"],
                        "titulo": titulo,
                        "resumo": resumo,
                        "link": link
                    })
        except Exception as e:
            print(f"[{datetime.now()}] Erro no feed {feed_info['nome']}: {e}")

    return noticias

def curar_noticias_perfil(noticias, nome_usuario, termos_perfil):
    if not noticias:
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)
    interesses_str = ", ".join(termos_perfil) if termos_perfil else "Geopolítica e Relações Internacionais"

    prompt = f"""
    Você é um Analista Sênior de Inteligência Estratégica.
    Relatório PERSONALIZADO para: {nome_usuario}.
    Interesses: {interesses_str}.

    Selecione até 5 notícias mais relevantes. Preserve o campo 'link' original no atributo 'url'.

    Array JSON esperado:
    [
      {{
        "topico": "Título",
        "fonte": "Fonte",
        "url": "Link original",
        "resumo_analitico": "**Fato:** ...\\n**Contexto:** ...\\n**Implicação:** ...",
        "relevancia": "Alta"
      }}
    ]

    Notícias:
    {json.dumps(noticias[:30], ensure_ascii=False)}
    """

    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)

    for tentativa in range(1, 4):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
            if response and response.text:
                return json.loads(response.text)
        except Exception as e:
            err_msg = str(e)
            print(f"[{datetime.now()}] Tentativa {tentativa} falhou para {nome_usuario}: {e}")
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                print(f"[{datetime.now()}] Cota estourada no Gemini (429). Pausando 30 segundos...")
                time.sleep(30)
            else:
                time.sleep(5)

    return []

def gerar_html_relatorio_individual(nome_usuario, topicos, termos_perfil):
    hoje = datetime.now(pytz.timezone("America/Sao_Paulo")).strftime("%d/%m/%Y")
    linhas_tabela = ""

    for idx, item in enumerate(topicos):
        bg_color = "#f9f9f9" if idx % 2 == 0 else "#ffffff"
        resumo_fmt = item['resumo_analitico'].replace('\n', '<br>')
        resumo_fmt = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', resumo_fmt)
        url_materia = item.get('url', item.get('link', '#'))

        linhas_tabela += f"""
        <tr style="background-color: {bg_color};">
            <td style="padding: 10px; border: 1px solid #dddddd; font-weight: bold; color: #2C3E50;">
                <a href="{url_materia}" target="_blank" style="color: #1a5276; text-decoration: underline;">{item['topico']} 🔗</a>
            </td>
            <td style="padding: 10px; border: 1px solid #dddddd;">{item['fonte']}</td>
            <td style="padding: 10px; border: 1px solid #dddddd; font-size: 13px; line-height: 1.4;">{resumo_fmt}</td>
            <td style="padding: 10px; border: 1px solid #dddddd; text-align: center; font-weight: bold;">{item.get('relevancia', 'Alta')}</td>
        </tr>
        """

    tags_interesses = " • ".join(termos_perfil) if termos_perfil else "Geral"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; margin: 0; padding: 20px;">
        <div style="max-width: 800px; margin: auto; background-color: #ffffff; padding: 25px; border-radius: 8px;">
            <div style="border-bottom: 2px solid #2C3E50; padding-bottom: 10px; margin-bottom: 20px;">
                <h2 style="color: #2C3E50; margin: 0;">Briefing Personalizado de Relações Internacionais</h2>
                <p style="color: #7f8c8d; font-size: 14px; margin: 5px 0 0 0;">Destinatário: <strong>{nome_usuario}</strong> • {hoje}</p>
                <p style="color: #2980b9; font-size: 12px; margin: 3px 0 0 0;">Filtros: {tags_interesses}</p>
            </div>
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                <thead>
                    <tr style="background-color: #2C3E50; color: #ffffff;">
                        <th style="padding: 12px; width: 30%;">Tópico</th>
                        <th style="padding: 12px; width: 15%;">Fonte</th>
                        <th style="padding: 12px; width: 43%;">Resumo Analítico</th>
                        <th style="padding: 12px; width: 12%;">Relevância</th>
                    </tr>
                </thead>
                <tbody>{linhas_tabela}</tbody>
            </table>
        </div>
    </body>
    </html>
    """

def processar_e_enviar_perfil(email, dados):
    if not dados.get("ativo", True):
        return

    nome = dados.get("nome", email)
    termos = dados.get("termos", [])

    print(f"[{datetime.now()}] Iniciando curadoria para: {nome} ({email})...")
    noticias = coletar_noticias_rss_para_perfil(termos)

    if not noticias:
        print(f"[{datetime.now()}] Nenhuma notícia encontrada para {nome}.")
        return

    curadoria = curar_noticias_perfil(noticias, nome, termos)

    if curadoria:
        html = gerar_html_relatorio_individual(nome, curadoria, termos)
        hoje = datetime.now(pytz.timezone("America/Sao_Paulo")).strftime("%d/%m/%Y")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Seu Briefing Diário de RI - {hoje}"
        msg["From"] = EMAIL_SENDER
        msg["To"] = email
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [email], msg.as_string())

        print(f"[{datetime.now()}] E-mail enviado com sucesso para: {email}")

def processar_todos_perfis():
    perfis = obter_perfis()
    for email, dados in perfis.items():
        try:
            processar_e_enviar_perfil(email, dados)
            time.sleep(15)
        except Exception as e:
            print(f"[{datetime.now()}] Erro no envio para {email}: {e}")

def agendador_loop():
    while True:
        try:
            agora_sp = datetime.now(pytz.timezone("America/Sao_Paulo"))
            hora_atual = agora_sp.strftime("%H:%M")
            if agora_sp.second < 10:
                perfis = obter_perfis()
                for email, dados in perfis.items():
                    if hora_atual in dados.get("horarios", []) and dados.get("ativo", True):
                        print(f"[{datetime.now()}] Disparando agendamento {hora_atual} -> {email}")
                        threading.Thread(target=processar_e_enviar_perfil, args=(email, dados), daemon=True).start()
                time.sleep(50)
        except Exception as e:
            print(f"[{datetime.now()}] Erro agendador: {e}")
        time.sleep(5)

TEMPLATE_LOGIN = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Curadoria RI</title>
    <style>
        :root {
            --bg-page: #080a0f;
            --bg-card: #10141d;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --border-color: #1e293b;
            --input-bg: #0b0e14;
            --btn-bg: #2563eb;
            --btn-hover: #1d4ed8;
        }
        [data-theme="light"] {
            --bg-page: #f0f4f8;
            --bg-card: #ffffff;
            --text-main: #1e293b;
            --text-sub: #64748b;
            --border-color: #cbd5e1;
            --input-bg: #ffffff;
            --btn-bg: #1e293b;
            --btn-hover: #334155;
        }
        body {
            font-family: system-ui, -apple-system, sans-serif;
            background: var(--bg-page);
            color: var(--text-main);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            transition: background 0.3s, color 0.3s;
        }
        .card {
            background: var(--bg-card);
            padding: 35px 30px;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            width: 100%;
            max-width: 380px;
            position: relative;
            border: 1px solid var(--border-color);
        }
        .theme-btn {
            position: absolute;
            top: 15px;
            right: 15px;
            background: transparent;
            border: 1px solid var(--border-color);
            color: var(--text-main);
            border-radius: 20px;
            padding: 4px 10px;
            font-size: 13px;
            cursor: pointer;
        }
        h2 { text-align: center; margin-top: 10px; margin-bottom: 25px; }
        label { font-size: 13px; font-weight: 600; margin-bottom: 5px; display: block; }
        input {
            width: 100%;
            padding: 10px 12px;
            margin-bottom: 18px;
            background: var(--input-bg);
            color: var(--text-main);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            box-sizing: border-box;
            font-size: 14px;
        }
        button[type="submit"] {
            width: 100%;
            padding: 12px;
            background: var(--btn-bg);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 15px;
            font-weight: bold;
            cursor: pointer;
            transition: background 0.2s;
        }
        button[type="submit"]:hover { background: var(--btn-hover); }
        .msg { color: #ef4444; font-size: 13px; text-align: center; margin-bottom: 15px; }
        .register-link { text-align: center; margin-top: 18px; font-size: 13.5px; }
        .register-link a { color: var(--btn-bg); text-decoration: none; font-weight: bold; }
        .register-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="card">
        <button type="button" class="theme-btn" onclick="toggleTheme()" id="theme-btn">☀️ Modo Claro</button>
        <h2>Curadoria RI</h2>
        {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
        <form action="/login" method="POST">
            <label>E-mail:</label>
            <input type="email" name="email" required placeholder="seu@email.com">
            <label>Senha:</label>
            <input type="password" name="senha" required placeholder="••••••••">
            <button type="submit">Acessar Painel</button>
        </form>
        <div class="register-link">
            Não tem uma conta? <a href="/register">Cadastre-se</a>
        </div>
    </div>
    <script>
        function applyTheme(t) {
            document.documentElement.setAttribute('data-theme', t);
            localStorage.setItem('theme', t);
            const btn = document.getElementById('theme-btn');
            if (btn) btn.innerHTML = t === 'light' ? '🌙 Modo Escuro' : '☀️ Modo Claro';
        }
        function toggleTheme() {
            const current = localStorage.getItem('theme') || 'dark';
            applyTheme(current === 'dark' ? 'light' : 'dark');
        }
        applyTheme(localStorage.getItem('theme') || 'dark');
    </script>
</body>
</html>
"""

TEMPLATE_REGISTER = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cadastro - Curadoria RI</title>
    <style>
        :root {
            --bg-page: #080a0f;
            --bg-card: #10141d;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --border-color: #1e293b;
            --input-bg: #0b0e14;
            --btn-bg: #2563eb;
            --btn-hover: #1d4ed8;
            --tag-bg: #2563eb;
            --tag-text: #ffffff;
        }
        [data-theme="light"] {
            --bg-page: #f0f4f8;
            --bg-card: #ffffff;
            --text-main: #1e293b;
            --text-sub: #64748b;
            --border-color: #cbd5e1;
            --input-bg: #ffffff;
            --btn-bg: #1e293b;
            --btn-hover: #334155;
            --tag-bg: #2563eb;
            --tag-text: #ffffff;
        }
        * { box-sizing: border-box; }
        body {
            font-family: system-ui, -apple-system, sans-serif;
            background: var(--bg-page);
            color: var(--text-main);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px 15px;
            transition: background 0.3s, color 0.3s;
        }
        .card {
            background: var(--bg-card);
            padding: 35px 30px;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            width: 100%;
            max-width: 480px;
            position: relative;
            border: 1px solid var(--border-color);
        }
        .theme-btn {
            position: absolute;
            top: 15px;
            right: 15px;
            background: transparent;
            border: 1px solid var(--border-color);
            color: var(--text-main);
            border-radius: 20px;
            padding: 4px 10px;
            font-size: 13px;
            cursor: pointer;
        }
        h2 { text-align: center; margin-top: 10px; margin-bottom: 25px; }
        .form-group { margin-bottom: 16px; }
        label { font-size: 13px; font-weight: 600; margin-bottom: 5px; display: block; }
        input[type="text"], input[type="email"], input[type="password"] {
            width: 100%;
            padding: 10px 12px;
            background: var(--input-bg);
            color: var(--text-main);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            font-size: 14px;
        }
        .tag-container {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            padding: 6px 10px;
            border: 1px solid var(--border-color);
            background-color: var(--input-bg);
            border-radius: 6px;
            min-height: 44px;
            cursor: text;
        }
        .tag-badge {
            display: inline-flex;
            align-items: center;
            background-color: var(--tag-bg);
            color: var(--tag-text);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 500;
        }
        .tag-badge .remove-tag {
            margin-left: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 14px;
            line-height: 1;
            opacity: 0.8;
        }
        .tag-badge .remove-tag:hover { opacity: 1; }
        .tag-input-field {
            border: none !important;
            outline: none !important;
            background: transparent !important;
            color: var(--text-main) !important;
            flex: 1;
            min-width: 120px;
            padding: 4px 0 !important;
            margin: 0 !important;
            font-size: 14px !important;
        }
        .hint-text {
            display: block;
            font-size: 11.5px;
            color: var(--text-sub);
            margin-top: 4px;
        }
        button[type="submit"] {
            width: 100%;
            padding: 12px;
            background: var(--btn-bg);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 15px;
            font-weight: bold;
            cursor: pointer;
            margin-top: 10px;
            transition: background 0.2s;
        }
        button[type="submit"]:hover { background: var(--btn-hover); }
        .msg { color: #ef4444; font-size: 13px; text-align: center; margin-bottom: 15px; }
        .login-link { text-align: center; margin-top: 18px; font-size: 13.5px; }
        .login-link a { color: var(--btn-bg); text-decoration: none; font-weight: bold; }
        .login-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="card">
        <button type="button" class="theme-btn" onclick="toggleTheme()" id="theme-btn">☀️ Modo Claro</button>
        <h2>Criar Nova Conta</h2>
        {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
        <form action="/register" method="POST">
            <div class="form-group">
                <label>Seu Nome Completo:</label>
                <input type="text" name="nome" required placeholder="Ex: Maria Silva">
            </div>

            <div class="form-group">
                <label>Seu E-mail:</label>
                <input type="email" name="email" required placeholder="seu@email.com">
            </div>

            <div class="form-group">
                <label>Sua Senha:</label>
                <input type="password" name="senha" required placeholder="••••••••">
            </div>

            <div class="form-group">
                <label>Filtros de Interesse:</label>
                <input type="hidden" name="termos" id="reg-termos-hidden" value="geopolitica,brics">
                <div class="tag-container" id="reg-tag-container"></div>
                <small class="hint-text">Pressione <strong>Enter</strong> ou <strong>,</strong> para adicionar termos.</small>
            </div>

            <div class="form-group">
                <label>Horários para Receber o Briefing:</label>
                <input type="hidden" name="horarios" id="reg-horarios-hidden" value="07:00">
                <div class="tag-container" id="reg-horarios-container"></div>
                <small class="hint-text">Pressione <strong>Enter</strong> ou <strong>,</strong> para adicionar o horário (ex: 07:00).</small>
            </div>

            <button type="submit">Cadastrar e Entrar</button>
        </form>
        <div class="login-link">
            Já possui uma conta? <a href="/login">Fazer Login</a>
        </div>
    </div>
    <script>
        function applyTheme(t) {
            document.documentElement.setAttribute('data-theme', t);
            localStorage.setItem('theme', t);
            const btn = document.getElementById('theme-btn');
            if (btn) btn.innerHTML = t === 'light' ? '🌙 Modo Escuro' : '☀️ Modo Claro';
        }
        function toggleTheme() {
            const current = localStorage.getItem('theme') || 'dark';
            applyTheme(current === 'dark' ? 'light' : 'dark');
        }
        applyTheme(localStorage.getItem('theme') || 'dark');

        function setupInteractiveTags(containerId, hiddenInputId, placeholderText = 'Digite e pressione Enter...') {
            const container = document.getElementById(containerId);
            const hiddenInput = document.getElementById(hiddenInputId);
            if (!container || !hiddenInput) return;

            let tags = hiddenInput.value ? hiddenInput.value.split(',').map(t => t.trim()).filter(Boolean) : [];

            const inputField = document.createElement('input');
            inputField.type = 'text';
            inputField.className = 'tag-input-field';
            inputField.placeholder = tags.length === 0 ? placeholderText : '';
            container.appendChild(inputField);

            function render() {
                container.querySelectorAll('.tag-badge').forEach(el => el.remove());

                tags.forEach((tagText, index) => {
                    const badge = document.createElement('span');
                    badge.className = 'tag-badge';
                    badge.textContent = tagText + ' ';

                    const removeBtn = document.createElement('span');
                    removeBtn.className = 'remove-tag';
                    removeBtn.innerHTML = '&times;';
                    removeBtn.onclick = function(e) {
                        e.stopPropagation();
                        tags.splice(index, 1);
                        render();
                    };

                    badge.appendChild(removeBtn);
                    container.insertBefore(badge, inputField);
                });

                hiddenInput.value = tags.join(',');
                inputField.placeholder = tags.length === 0 ? placeholderText : '';
            }

            inputField.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    const val = inputField.value.trim().replace(/,/g, '');
                    if (val && !tags.includes(val)) {
                        tags.push(val);
                        inputField.value = '';
                        render();
                    }
                } else if (e.key === 'Backspace' && inputField.value === '' && tags.length > 0) {
                    tags.pop();
                    render();
                }
            });

            container.addEventListener('click', function() {
                inputField.focus();
            });

            render();
        }

        document.addEventListener('DOMContentLoaded', function() {
            setupInteractiveTags('reg-tag-container', 'reg-termos-hidden', 'Digite um termo e pressione Enter...');
            setupInteractiveTags('reg-horarios-container', 'reg-horarios-hidden', 'Digite o horário (ex: 07:00)...');
        });
    </script>
</body>
</html>
"""

TEMPLATE_PAINEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Curadoria RI</title>
    <style>
        :root {
            --bg-page: #080a0f;
            --bg-card: #10141d;
            --bg-section: #0b0e14;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
            --border-color: #1e293b;
            --input-bg: #0b0e14;
            --btn-header: #1e293b;
            --btn-save: #2563eb;
            --btn-test: #16a34a;
            --btn-danger: #ef4444;
            --tag-bg: #2563eb;
            --tag-text: #ffffff;
            --msg-bg: #14532d;
            --msg-text: #dcfce7;
        }

        [data-theme="light"] {
            --bg-page: #f1f5f9;
            --bg-card: #ffffff;
            --bg-section: #f8fafc;
            --text-main: #1e293b;
            --text-sub: #64748b;
            --border-color: #cbd5e1;
            --input-bg: #ffffff;
            --btn-header: #475569;
            --btn-save: #1e293b;
            --btn-test: #16a34a;
            --btn-danger: #dc2626;
            --tag-bg: #2563eb;
            --tag-text: #ffffff;
            --msg-bg: #dcfce7;
            --msg-text: #166534;
        }

        * { box-sizing: border-box; }

        body {
            font-family: system-ui, -apple-system, sans-serif;
            background-color: var(--bg-page);
            color: var(--text-main);
            margin: 0;
            padding: 30px 15px;
            display: flex;
            justify-content: center;
            transition: background-color 0.3s, color 0.3s;
        }

        .container {
            background-color: var(--bg-card);
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            width: 100%;
            max-width: 800px;
            padding: 30px;
            border: 1px solid var(--border-color);
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 15px;
            margin-bottom: 25px;
        }

        .header h2 { margin: 0; font-size: 24px; color: var(--text-main); }
        .header small { color: var(--text-sub); font-size: 14px; }

        .header-actions { display: flex; gap: 10px; align-items: center; }

        .btn-theme, .btn-logout {
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            border: 1px solid var(--border-color);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }

        .btn-theme { background: var(--btn-header); color: #ffffff; border: none; }
        .btn-logout { background: transparent; color: var(--text-main); }
        .btn-logout:hover { background: var(--bg-section); }

        .section {
            background-color: var(--bg-section);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 25px;
        }

        .section-title {
            margin-top: 0;
            margin-bottom: 18px;
            font-size: 18px;
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--text-main);
        }

        .form-row {
            display: flex;
            gap: 15px;
            margin-bottom: 15px;
        }

        .form-group {
            flex: 1;
            margin-bottom: 15px;
        }

        label {
            display: block;
            font-size: 13.5px;
            font-weight: 600;
            margin-bottom: 6px;
            color: var(--text-main);
        }

        input[type="text"], input[type="email"], input[type="password"], select {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid var(--border-color);
            background-color: var(--input-bg);
            color: var(--text-main);
            border-radius: 6px;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
        }

        input:focus { border-color: var(--tag-bg); }

        .tag-container {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            padding: 6px 10px;
            border: 1px solid var(--border-color);
            background-color: var(--input-bg);
            border-radius: 6px;
            min-height: 44px;
            cursor: text;
        }

        .tag-badge {
            display: inline-flex;
            align-items: center;
            background-color: var(--tag-bg);
            color: var(--tag-text);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 500;
        }

        .tag-badge .remove-tag {
            margin-left: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 14px;
            line-height: 1;
            opacity: 0.8;
        }
        .tag-badge .remove-tag:hover { opacity: 1; }

        .tag-input-field {
            border: none !important;
            outline: none !important;
            background: transparent !important;
            color: var(--text-main) !important;
            flex: 1;
            min-width: 150px;
            padding: 4px 0 !important;
            margin: 0 !important;
            font-size: 14px !important;
        }

        .hint-text {
            display: block;
            font-size: 12px;
            color: var(--text-sub);
            margin-top: 5px;
        }

        .btn-save {
            background-color: var(--btn-save);
            color: white;
            border: none;
            padding: 11px 18px;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            font-size: 14px;
            transition: opacity 0.2s;
        }

        .btn-test {
            background-color: var(--btn-test);
            color: white;
            border: none;
            padding: 11px 18px;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            font-size: 14px;
            margin-top: 10px;
            transition: opacity 0.2s;
        }

        .btn-save:hover, .btn-test:hover { opacity: 0.9; }

        .msg-banner {
            background-color: var(--msg-bg);
            color: var(--msg-text);
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 20px;
            text-align: center;
            font-weight: 600;
            font-size: 14px;
        }

        .card-user {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            padding: 14px;
            border-radius: 8px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .user-info { display: flex; flex-direction: column; gap: 3px; }
        .user-info strong { font-size: 15px; }
        .user-info span { font-size: 12px; color: var(--text-sub); }

        .user-actions { display: flex; gap: 8px; }

        .btn-sm-test { background: var(--btn-test); color: white; border: none; padding: 6px 12px; border-radius: 4px; font-size: 12px; cursor: pointer; font-weight: bold; }
        .btn-sm-del { background: var(--btn-danger); color: white; border: none; padding: 6px 12px; border-radius: 4px; font-size: 12px; cursor: pointer; font-weight: bold; }

        @media (max-width: 600px) {
            .form-row { flex-direction: column; gap: 0; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h2>Curadoria RI</h2>
                <small>Olá, <b>{{ user.nome }}</b> ({{ 'Administrador' if user.role == 'admin' else 'Perfil Usuário' }})</small>
            </div>
            <div class="header-actions">
                <button type="button" class="btn-theme" id="btn-theme" onclick="toggleTheme()">☀️ Modo Claro</button>
                <a href="/logout" class="btn-logout">🚪 Sair</a>
            </div>
        </div>

        {% if mensagem %}
            <div class="msg-banner">{{ mensagem }}</div>
        {% endif %}

        <div class="section">
            <h3 class="section-title">⚙️ Suas Configurações Individuais</h3>
            <form action="/salvar-meu-perfil" method="POST">
                <div class="form-row">
                    <div class="form-group">
                        <label>Seu Nome:</label>
                        <input type="text" name="nome" value="{{ user.nome }}" required>
                    </div>
                    <div class="form-group">
                        <label>Alterar Senha (opcional):</label>
                        <input type="password" name="nova_senha" placeholder="Nova senha se quiser mudar">
                    </div>
                </div>

                <div class="form-group">
                    <label>Seus Filtros de Interesse:</label>
                    <input type="hidden" name="termos" id="user-termos-hidden" value="{{ user.termos | join(',') }}">
                    <div class="tag-container" id="user-tag-container"></div>
                    <small class="hint-text">Digite o termo e pressione <strong>Enter</strong> ou <strong>vírgula (,)</strong> para adicionar. Clique no <strong>×</strong> para remover.</small>
                </div>

                <div class="form-group">
                    <label>Seus Horários de Recebimento (ex: 07:00, 13:00):</label>
                    <input type="hidden" name="horarios" id="user-horarios-hidden" value="{{ user.horarios | join(',') }}">
                    <div class="tag-container" id="user-horarios-tag-container"></div>
                    <small class="hint-text">Digite o horário (ex: 07:00) e pressione <strong>Enter</strong> ou <strong>vírgula (,)</strong> para adicionar.</small>
                </div>

                <button type="submit" class="btn-save">💾 Salvar Minhas Preferências</button>
            </form>

            <form action="/testar-meu-perfil" method="POST">
                <button type="submit" class="btn-test">🚀 Disparar Teste para Meu E-mail Agora</button>
            </form>
        </div>

        {% if user.role == 'admin' %}
            <div class="section">
                <h3 class="section-title">👑 Painel Administrador</h3>

                <form action="/executar-todos" method="POST" style="margin-bottom: 25px;">
                    <button type="submit" class="btn-test" style="width: 100%; font-size: 15px;">⚡ Disparar Curadoria Geral Para Todos</button>
                </form>

                <h4 style="margin-bottom: 15px;">➕ Criar / Atualizar Usuário</h4>
                <form action="/admin/salvar-usuario" method="POST" style="margin-bottom: 30px;">
                    <div class="form-row">
                        <div class="form-group">
                            <label>Nome Completo:</label>
                            <input type="text" name="nome" required placeholder="Nome do Usuário">
                        </div>
                        <div class="form-group">
                            <label>E-mail:</label>
                            <input type="email" name="email" required placeholder="usuario@email.com">
                        </div>
                    </div>

                    <div class="form-row">
                        <div class="form-group">
                            <label>Senha:</label>
                            <input type="password" name="senha" placeholder="Senha inicial">
                        </div>
                        <div class="form-group">
                            <label>Tipo de Conta:</label>
                            <select name="role">
                                <option value="user">Usuário Comum</option>
                                <option value="admin">Administrador</option>
                            </select>
                        </div>
                    </div>

                    <div class="form-group">
                        <label>Filtros de Interesse do Usuário:</label>
                        <input type="hidden" name="termos" id="admin-termos-hidden" value="">
                        <div class="tag-container" id="admin-tag-container"></div>
                        <small class="hint-text">Pressione <strong>Enter</strong> ou <strong>,</strong> para adicionar cada palavra-chave.</small>
                    </div>

                    <div class="form-group">
                        <label>Horários (ex: 07:00, 13:00):</label>
                        <input type="hidden" name="horarios" id="admin-horarios-hidden" value="">
                        <div class="tag-container" id="admin-horarios-tag-container"></div>
                        <small class="hint-text">Pressione <strong>Enter</strong> ou <strong>,</strong> para adicionar o horário.</small>
                    </div>

                    <button type="submit" class="btn-save">➕ Cadastrar / Atualizar Perfil</button>
                </form>

                <h4>👥 Perfis Cadastrados ({{ todos_perfis|length }})</h4>
                {% for email, p in todos_perfis.items() %}
                    <div class="card-user">
                        <div class="user-info">
                            <strong>{{ p.nome }} {% if p.role == 'admin' %}👑{% endif %}</strong>
                            <span>{{ email }} • Horários: {{ p.horarios | join(', ') }}</span>
                            <span style="font-size: 11px; opacity: 0.8;">Filtros: {{ p.termos | join(', ') if p.termos else 'Nenhum' }}</span>
                        </div>
                        <div class="user-actions">
                            <form action="/admin/testar-perfil" method="POST" style="margin: 0;">
                                <input type="hidden" name="email" value="{{ email }}">
                                <button type="submit" class="btn-sm-test">🚀 Testar</button>
                            </form>
                            {% if email != user.email %}
                                <form action="/admin/remover-usuario" method="POST" style="margin: 0;">
                                    <input type="hidden" name="email" value="{{ email }}">
                                    <button type="submit" class="btn-sm-del">Excluir</button>
                                </form>
                            {% endif %}
                        </div>
                    </div>
                {% endfor %}
            </div>
        {% endif %}
    </div>

    <script>
        function applyTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('theme', theme);
            const btn = document.getElementById('btn-theme');
            if (btn) {
                btn.innerHTML = theme === 'dark' ? '☀️ Modo Claro' : '🌙 Modo Escuro';
            }
        }

        function toggleTheme() {
            const currentTheme = localStorage.getItem('theme') || 'dark';
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            applyTheme(newTheme);
        }

        const savedTheme = localStorage.getItem('theme') || 'dark';
        applyTheme(savedTheme);

        function setupInteractiveTags(containerId, hiddenInputId, placeholderText = 'Digite um termo e pressione Enter...') {
            const container = document.getElementById(containerId);
            const hiddenInput = document.getElementById(hiddenInputId);
            if (!container || !hiddenInput) return;

            let tags = hiddenInput.value ? hiddenInput.value.split(',').map(t => t.trim()).filter(Boolean) : [];

            const inputField = document.createElement('input');
            inputField.type = 'text';
            inputField.className = 'tag-input-field';
            inputField.placeholder = tags.length === 0 ? placeholderText : '';
            container.appendChild(inputField);

            function render() {
                container.querySelectorAll('.tag-badge').forEach(el => el.remove());

                tags.forEach((tagText, index) => {
                    const badge = document.createElement('span');
                    badge.className = 'tag-badge';
                    badge.textContent = tagText + ' ';

                    const removeBtn = document.createElement('span');
                    removeBtn.className = 'remove-tag';
                    removeBtn.innerHTML = '&times;';
                    removeBtn.onclick = function(e) {
                        e.stopPropagation();
                        tags.splice(index, 1);
                        render();
                    };

                    badge.appendChild(removeBtn);
                    container.insertBefore(badge, inputField);
                });

                hiddenInput.value = tags.join(',');
                inputField.placeholder = tags.length === 0 ? placeholderText : '';
            }

            inputField.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    const val = inputField.value.trim().replace(/,/g, '');
                    if (val && !tags.includes(val)) {
                        tags.push(val);
                        inputField.value = '';
                        render();
                    }
                } else if (e.key === 'Backspace' && inputField.value === '' && tags.length > 0) {
                    tags.pop();
                    render();
                }
            });

            container.addEventListener('click', function() {
                inputField.focus();
            });

            render();
        }

        document.addEventListener('DOMContentLoaded', function() {
            setupInteractiveTags('user-tag-container', 'user-termos-hidden', 'Digite um termo e pressione Enter...');
            setupInteractiveTags('user-horarios-tag-container', 'user-horarios-hidden', 'Digite o horário (ex: 07:00)...');
            setupInteractiveTags('admin-tag-container', 'admin-termos-hidden', 'Digite um termo e pressione Enter...');
            setupInteractiveTags('admin-horarios-tag-container', 'admin-horarios-hidden', 'Digite o horário (ex: 07:00)...');
        });
    </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    user = obter_usuario_logado()
    if not user:
        return redirect("/login")
    return render_template_string(TEMPLATE_PAINEL, user=user, todos_perfis=obter_perfis(), mensagem=request.args.get("msg"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        perfis = obter_perfis()
        user = perfis.get(email)
        if user and check_password_hash(user.get("senha_hash", ""), senha):
            session["user_email"] = email
            return redirect("/")
        return render_template_string(TEMPLATE_LOGIN, msg="E-mail ou senha inválidos.")
    return render_template_string(TEMPLATE_LOGIN)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        nome = request.form.get("nome", "").strip()
        senha = request.form.get("senha", "").strip()
        termos_raw = request.form.get("termos", "").strip()
        horarios_raw = request.form.get("horarios", "").strip()

        if not email or not nome or not senha:
            return render_template_string(TEMPLATE_REGISTER, msg="Por favor, preencha nome, e-mail e senha.")

        perfis = obter_perfis()
        if email in perfis:
            return render_template_string(TEMPLATE_REGISTER, msg="Este e-mail já está cadastrado. Faça login.")

        termos = [t.strip().lower() for t in termos_raw.split(",") if t.strip()]
        horarios = [h.strip() for h in horarios_raw.split(",") if re.match(r'^\d{2}:\d{2}$', h.strip())]

        perfis[email] = {
            "nome": nome,
            "email": email,
            "senha_hash": generate_password_hash(senha),
            "role": "user",
            "termos": termos if termos else ["geopolitica"],
            "horarios": horarios if horarios else ["07:00"],
            "ativo": True
        }
        salvar_perfis(perfis)

        session["user_email"] = email
        return redirect("/?msg=Conta criada com sucesso! Seja bem-vindo(a).")

    return render_template_string(TEMPLATE_REGISTER)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/salvar-meu-perfil", methods=["POST"])
def salvar_meu_perfil():
    user = obter_usuario_logado()
    if not user:
        return redirect("/login")

    perfis = obter_perfis()
    email = user["email"]
    nome = request.form.get("nome", "").strip()
    nova_senha = request.form.get("nova_senha", "").strip()
    termos_raw = request.form.get("termos", "").strip()
    horarios_raw = request.form.get("horarios", "").strip()

    if nome:
        perfis[email]["nome"] = nome
        if nova_senha:
            perfis[email]["senha_hash"] = generate_password_hash(nova_senha)
        perfis[email]["termos"] = [t.strip().lower() for t in termos_raw.split(",") if t.strip()]
        horarios = [h.strip() for h in horarios_raw.split(",") if re.match(r'^\d{2}:\d{2}$', h.strip())]
        perfis[email]["horarios"] = horarios if horarios else ["07:00"]
        salvar_perfis(perfis)
        msg = "Suas configurações foram atualizadas com sucesso!"
    else:
        msg = "Erro ao salvar."

    return redirect(f"/?msg={msg}")

@app.route("/testar-meu-perfil", methods=["POST"])
def testar_meu_perfil():
    user = obter_usuario_logado()
    if user:
        threading.Thread(target=processar_e_enviar_perfil, args=(user["email"], user), daemon=True).start()
        msg = "Envio de teste iniciado! Verifique sua caixa de entrada em instantes."
    else:
        msg = "Sessão inválida."
    return redirect(f"/?msg={msg}")

@app.route("/admin/salvar-usuario", methods=["POST"])
def admin_salvar_usuario():
    user = obter_usuario_logado()
    if not user or user.get("role") != "admin":
        return "Acesso Negado", 403

    email = request.form.get("email", "").strip().lower()
    nome = request.form.get("nome", "").strip()
    senha = request.form.get("senha", "").strip()
    role = request.form.get("role", "user")
    termos_raw = request.form.get("termos", "").strip()
    horarios_raw = request.form.get("horarios", "").strip()

    if email and nome:
        perfis = obter_perfis()
        senha_hash = generate_password_hash(senha) if senha else perfis.get(email, {}).get("senha_hash", generate_password_hash("123456"))
        termos = [t.strip().lower() for t in termos_raw.split(",") if t.strip()]
        horarios = [h.strip() for h in horarios_raw.split(",") if re.match(r'^\d{2}:\d{2}$', h.strip())]

        perfis[email] = {
            "nome": nome,
            "email": email,
            "senha_hash": senha_hash,
            "role": role,
            "termos": termos,
            "horarios": horarios if horarios else ["07:00"],
            "ativo": True
        }
        salvar_perfis(perfis)
        msg = f"Perfil de {nome} salvo com sucesso!"
    else:
        msg = "Erro nos dados do formulário."

    return redirect(f"/?msg={msg}")

@app.route("/admin/remover-usuario", methods=["POST"])
def admin_remover_usuario():
    user = obter_usuario_logado()
    if not user or user.get("role") != "admin":
        return "Acesso Negado", 403

    email = request.form.get("email", "").strip().lower()
    perfis = obter_perfis()
    if email in perfis and email != user["email"]:
        del perfis[email]
        salvar_perfis(perfis)
        msg = "Perfil excluído com sucesso."
    else:
        msg = "Operação inválida."
    return redirect(f"/?msg={msg}")

@app.route("/admin/testar-perfil", methods=["POST"])
def admin_testar_perfil():
    user = obter_usuario_logado()
    if not user or user.get("role") != "admin":
        return "Acesso Negado", 403

    email = request.form.get("email", "").strip().lower()
    perfis = obter_perfis()
    if email in perfis:
        threading.Thread(target=processar_e_enviar_perfil, args=(email, perfis[email]), daemon=True).start()
        msg = f"Teste iniciado para {email}!"
    else:
        msg = "Perfil não encontrado."
    return redirect(f"/?msg={msg}")

@app.route("/executar-todos", methods=["POST"])
def executar_todos():
    user = obter_usuario_logado()
    if not user or user.get("role") != "admin":
        return "Acesso Negado", 403

    threading.Thread(target=processar_todos_perfis, daemon=True).start()
    return redirect("/?msg=Curadoria geral iniciada em segundo plano para todos os perfis!")

if __name__ == "__main__":
    obter_perfis()
    threading.Thread(target=agendador_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
