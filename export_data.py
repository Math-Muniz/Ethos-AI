#!/usr/bin/env python3
"""
Script para exportar dados do conversation_log para análise.

Uso:
    python export_data.py --format csv --output dados.csv
    python export_data.py --format json --output dados.json
    python export_data.py --thread-id <uuid> --output conversa.csv
"""

import os
import sys
import csv
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

load_dotenv()

def create_connection():
    """Cria conexão com o banco de dados."""
    return psycopg.connect(
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST"),
        port=os.getenv("POSTGRES_PORT"),
        dbname=os.getenv("POSTGRES_DB"),
    )

def export_all_messages(output_file, format='csv'):
    """Exporta todas as mensagens."""
    query = """
        SELECT
            cl.id,
            cl.thread_id,
            cl.user_id,
            cl.persona_name,
            cl.session_number,
            cl.message_type,
            cl.message_content,
            LENGTH(cl.message_content) as message_length,
            cl.message_order,
            cl.timestamp,
            EXTRACT(HOUR FROM cl.timestamp) as hour_of_day,
            EXTRACT(DOW FROM cl.timestamp) as day_of_week
        FROM conversation_log cl
        ORDER BY cl.thread_id, cl.message_order
    """

    conn = create_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            results = cur.fetchall()

            if format == 'csv':
                export_to_csv(results, output_file)
            elif format == 'json':
                export_to_json(results, output_file)

            print(f"✅ Exportados {len(results)} registros para {output_file}")
    finally:
        conn.close()

def export_thread(thread_id, output_file):
    """Exporta uma conversa específica."""
    query = """
        SELECT
            id,
            thread_id,
            persona_name,
            session_number,
            message_type,
            message_content,
            message_order,
            timestamp
        FROM conversation_log
        WHERE thread_id = %s
        ORDER BY message_order
    """

    conn = create_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (thread_id,))
            results = cur.fetchall()

            export_to_csv(results, output_file)
            print(f"✅ Exportada conversa {thread_id} para {output_file}")
    finally:
        conn.close()

def export_summary(output_file):
    """Exporta resumo estatístico."""
    query = """
        SELECT
            persona_name,
            session_number,
            message_type,
            COUNT(*) as total_messages,
            AVG(LENGTH(message_content)) as avg_length,
            MIN(LENGTH(message_content)) as min_length,
            MAX(LENGTH(message_content)) as max_length,
            COUNT(DISTINCT user_id) as unique_users
        FROM conversation_log
        GROUP BY persona_name, session_number, message_type
        ORDER BY persona_name, session_number, message_type
    """

    conn = create_connection()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            results = cur.fetchall()

            export_to_csv(results, output_file)
            print(f"✅ Exportado resumo para {output_file}")
    finally:
        conn.close()

def export_to_csv(data, filename):
    """Exporta dados para CSV."""
    if not data:
        print("⚠️ Nenhum dado para exportar")
        return

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

def export_to_json(data, filename):
    """Exporta dados para JSON."""
    # Converter datetime para string
    for row in data:
        for key, value in row.items():
            if isinstance(value, datetime):
                row[key] = value.isoformat()

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def main():
    parser = argparse.ArgumentParser(description='Exportar dados do conversation_log')
    parser.add_argument('--format', choices=['csv', 'json'], default='csv',
                        help='Formato de saída (padrão: csv)')
    parser.add_argument('--output', required=True,
                        help='Arquivo de saída')
    parser.add_argument('--thread-id',
                        help='Exportar apenas uma conversa específica')
    parser.add_argument('--summary', action='store_true',
                        help='Exportar resumo estatístico ao invés de dados completos')

    args = parser.parse_args()

    try:
        if args.thread_id:
            export_thread(args.thread_id, args.output)
        elif args.summary:
            export_summary(args.output)
        else:
            export_all_messages(args.output, args.format)

    except Exception as e:
        print(f"❌ Erro ao exportar: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
