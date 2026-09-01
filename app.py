import os
import json
import sqlite3
from flask import Flask, jsonify

app = Flask(__name__)
DB_PATH = "/app/data/ach_database.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return jsonify({"status": "Serviço RI-Relatórios Ativo"})

@app.route('/api/ach/ultima', methods=['GET'])
def ultima_analise_ach():
    if not os.path.exists(DB_PATH):
        return jsonify({"status": "erro", "mensagem": "Banco de dados ainda não foi inicializado."}), 404

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, data_execucao, resultado_json FROM analises_ach ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({"status": "erro", "mensagem": "Nenhuma análise ACH registrada."}), 404

        return jsonify({
            "status": "sucesso",
            "id": row["id"],
            "data_execucao": row["data_execucao"],
            "matriz": json.loads(row["resultado_json"])
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"FalhZ ao consultar banco: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
