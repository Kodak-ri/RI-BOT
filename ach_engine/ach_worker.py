import os
import time
import json
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import schedule
from datetime import datetime
from google import genai
from ingestion import coletar_noticias_osint

DB_PATH = "/app/data/ach_database.db"

HIPOTESES_ACH = [
    "H1: Status Quo & Diplomacia Estável",
    "H2: Restrições de Hardware & Licenciamento EUA",
    "H3: Retaliação de Insumos pela China",
    "H4: Ação de Zona Cinzenta / Bloqueio Aduaneiro",
    "H5: Escalada Militar Direta / Conflito Armado"
]

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS analises_ach (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_execucao TEXT,
                resultado_json TEXT
            )
        ''')

def enviar_email(assunto, corpo_texto):
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if not all([smtp_user, smtp_pass, email_to]):
        print("⚠️ Variáveis de e-mail ausentes. Envio cancelado.")
        return

    msg = MIMEMultipart()
    msg['From'] = smtp_user
    msg['To'] = email_to
    msg['Subject'] = assunto
    msg.attach(MIMEText(corpo_texto, 'plain', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print("📧 E-mail de resumo enviado com sucesso!")
    except Exception as e:
        print(f"❌ Erro ao enviar e-mail: {e}")

def formatar_corpo_email(dados_json, data_execucao):
    texto = f"RELATÓRIO DE ANÁLISE ACH - {data_execucao}\n"
    texto += "=" * 50 + "\n\n"
    
    for item in dados_json.get("hipoteses", []):
        texto += f"[{item.get('id')}] {item.get('nome')}\n"
        texto += f"• Tendência: {item.get('tendencia')}\n"
        texto += f"• Probabilidade: {item.get('probabilidade')}%\n"
        texto += f"• Justificativa: {item.get('justificativa')}\n"
        texto += "-" * 40 + "\n"
    return texto

def analisar_matriz_ach(noticias):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: return

    client = genai.Client(api_key=api_key)
    amostra = "\n".join([f"- [{n['pilar']}] {n['titulo']}" for n in noticias[:25]])

    prompt = f"""
    Avalie o impacto das notícias nas 5 Hipóteses ACH: {', '.join(HIPOTESES_ACH)}.
    Notícias: {amostra}
    Retorne um JSON com a estrutura exata:
    {{
      "hipoteses": [
        {{"id": "H1", "nome": "Status Quo & Diplomacia Estável", "tendencia": "Neutra", "probabilidade": 55, "justificativa": "..."}}
      ]
    }}
    """

    resposta = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config={'response_mime_type': 'application/json'}
    )
    return resposta.text

def rodar_pipeline():
    print(f"[{datetime.now()}] 🚀 Executando pipeline OSINT + ACH...")
    init_db()
    noticias = coletar_noticias_osint(dias_janela=7)
    
    if noticias:
        json_raw = analisar_matriz_ach(noticias)
        data_atual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO analises_ach (data_execucao, resultado_json) VALUES (?, ?)", (data_atual, json_raw))
        
        print(f"[{datetime.now()}] ✅ Análise gravada no SQLite.")
        
        try:
            dados_dict = json.loads(json_raw)
            corpo = formatar_corpo_email(dados_dict, data_atual)
            enviar_email(f"[ALERT ACH] Relatório Matriz Geopolítica - {data_atual}", corpo)
        except Exception as e:
            print(f"⚠️ Erro ao formatar e-mail: {e}")

schedule.every().monday.at("06:00").do(rodar_pipeline)

if __name__ == "__main__":
    rodar_pipeline()
    while True:
        schedule.run_pending()
        time.sleep(60)
