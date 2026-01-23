# 📊 Guia de Análise de Dados - Dissertação

Este documento mostra como usar a tabela `conversation_log` para análise dos dados da sua pesquisa de mestrado.

## 🎯 O que foi implementado

### Tabela `conversation_log`

Todas as mensagens são automaticamente salvas em formato tabular:

```sql
conversation_log:
  - id: Identificador único
  - thread_id: ID da simulação
  - user_id: ID do aluno
  - persona_name: Nome do paciente (Clara, Luiz, Rafael)
  - session_number: Número da sessão (1-7)
  - message_type: Tipo da mensagem (therapist, patient, evaluation)
  - message_content: Conteúdo completo da mensagem
  - message_order: Ordem da mensagem na conversa
  - timestamp: Data/hora da mensagem
  - metadata: Dados extras em JSON
```

---

## 📋 Como Acessar no Supabase

1. Acesse [https://supabase.com](https://supabase.com)
2. Entre no seu projeto
3. Vá em **Table Editor** → `conversation_log`
4. Para exportar: Clique no botão **"Export to CSV"**

---

## 🔍 Queries SQL para Análise

### 1. Visão Geral da Pesquisa

```sql
-- Total de mensagens por tipo
SELECT
    message_type,
    COUNT(*) as total_messages,
    COUNT(DISTINCT thread_id) as simulacoes,
    COUNT(DISTINCT user_id) as alunos
FROM conversation_log
GROUP BY message_type
ORDER BY total_messages DESC;
```

### 2. Análise por Persona

```sql
-- Engajamento por persona
SELECT
    persona_name,
    COUNT(*) as total_messages,
    AVG(LENGTH(message_content)) as avg_message_length,
    COUNT(DISTINCT user_id) as alunos_unicos,
    COUNT(DISTINCT thread_id) as simulacoes_completas
FROM conversation_log
WHERE message_type IN ('therapist', 'patient')
GROUP BY persona_name
ORDER BY total_messages DESC;
```

### 3. Análise de Sessões

```sql
-- Taxa de conclusão por sessão
SELECT
    session_number,
    COUNT(DISTINCT thread_id) as iniciadas,
    COUNT(DISTINCT CASE
        WHEN message_type = 'evaluation'
        THEN thread_id
    END) as completas,
    ROUND(100.0 * COUNT(DISTINCT CASE
        WHEN message_type = 'evaluation'
        THEN thread_id
    END) / COUNT(DISTINCT thread_id), 2) as taxa_conclusao
FROM conversation_log
GROUP BY session_number
ORDER BY session_number;
```

### 4. Análise Temporal

```sql
-- Distribuição de uso por hora do dia
SELECT
    EXTRACT(HOUR FROM timestamp) as hora,
    COUNT(*) as total_messages,
    COUNT(DISTINCT user_id) as alunos_ativos
FROM conversation_log
GROUP BY hora
ORDER BY hora;
```

### 5. Análise por Aluno

```sql
-- Métricas individuais por aluno
SELECT
    user_id,
    COUNT(DISTINCT thread_id) as simulacoes_iniciadas,
    COUNT(DISTINCT CASE
        WHEN message_type = 'evaluation'
        THEN thread_id
    END) as simulacoes_completas,
    COUNT(*) as total_messages,
    MIN(timestamp) as primeira_interacao,
    MAX(timestamp) as ultima_interacao
FROM conversation_log
GROUP BY user_id
ORDER BY total_messages DESC;
```

### 6. Análise de Comprimento de Mensagens

```sql
-- Estatísticas de tamanho de mensagens
SELECT
    message_type,
    persona_name,
    session_number,
    AVG(LENGTH(message_content)) as media_caracteres,
    MIN(LENGTH(message_content)) as minimo,
    MAX(LENGTH(message_content)) as maximo,
    COUNT(*) as total_messages
FROM conversation_log
WHERE message_type IN ('therapist', 'patient')
GROUP BY message_type, persona_name, session_number
ORDER BY persona_name, session_number, message_type;
```

### 7. Análise de Avaliações

```sql
-- Todas as avaliações por sessão
SELECT
    thread_id,
    persona_name,
    session_number,
    message_content as avaliacao,
    timestamp
FROM conversation_log
WHERE message_type = 'evaluation'
ORDER BY timestamp DESC;
```

### 8. Análise de Abandono

```sql
-- Identificar simulações abandonadas
SELECT
    cl.thread_id,
    cl.user_id,
    cl.persona_name,
    MAX(cl.session_number) as ultima_sessao,
    MAX(cl.timestamp) as ultima_atividade,
    CASE
        WHEN MAX(cl.session_number) < 7 THEN 'Abandonada'
        ELSE 'Completa'
    END as status
FROM conversation_log cl
GROUP BY cl.thread_id, cl.user_id, cl.persona_name
HAVING MAX(cl.session_number) < 7
ORDER BY ultima_atividade DESC;
```

### 9. Análise de Checklist (Avaliações)

```sql
-- Contar itens marcados nas avaliações
SELECT
    persona_name,
    session_number,
    message_content,
    (LENGTH(message_content) - LENGTH(REPLACE(LOWER(message_content), '[x]', ''))) / 3 as itens_marcados
FROM conversation_log
WHERE message_type = 'evaluation'
ORDER BY session_number, persona_name;
```

### 10. Export Completo para Análise Estatística

```sql
-- Dados consolidados para SPSS/Python/R
SELECT
    cl.id,
    cl.thread_id,
    cl.user_id,
    cl.persona_name,
    cl.session_number,
    cl.message_type,
    LENGTH(cl.message_content) as message_length,
    cl.message_order,
    cl.timestamp,
    EXTRACT(HOUR FROM cl.timestamp) as hour_of_day,
    EXTRACT(DOW FROM cl.timestamp) as day_of_week,
    CASE
        WHEN sm.session_complete THEN 'Complete'
        ELSE 'Incomplete'
    END as simulation_status
FROM conversation_log cl
LEFT JOIN session_metadata sm ON cl.thread_id = sm.thread_id
ORDER BY cl.thread_id, cl.message_order;
```

---

## 📊 Como Exportar para Análise

### Para Excel/SPSS

1. No Supabase, execute a query desejada
2. Clique em **"Download as CSV"**
3. Abra no Excel ou importe no SPSS

### Para Python (pandas)

```python
import psycopg2
import pandas as pd

# Conectar ao banco
conn = psycopg2.connect(
    host="seu-host.supabase.co",
    database="postgres",
    user="postgres",
    password="sua-senha"
)

# Carregar dados
query = """
SELECT * FROM conversation_log
ORDER BY thread_id, message_order
"""
df = pd.read_csv(query, conn)

# Análises
print(df.groupby('persona_name')['message_length'].describe())
df.to_csv('dados_dissertacao.csv', index=False)
```

### Para R

```r
library(RPostgreSQL)
library(dplyr)

# Conectar ao banco
con <- dbConnect(
  PostgreSQL(),
  host = "seu-host.supabase.co",
  dbname = "postgres",
  user = "postgres",
  password = "sua-senha"
)

# Carregar dados
dados <- dbGetQuery(con, "SELECT * FROM conversation_log")

# Análises
summary(dados)
write.csv(dados, "dados_dissertacao.csv", row.names = FALSE)
```

---

## 🔒 Backup Manual

Se quiser fazer um backup completo do banco:

1. Acesse o Supabase Dashboard
2. Vá em **Settings** → **Database**
3. Clique em **Database Backups**
4. Faça download do último backup

Ou via SQL:

```sql
-- Exportar todos os dados
COPY (
  SELECT * FROM conversation_log
) TO '/tmp/conversation_log_backup.csv' WITH CSV HEADER;
```

---

## 📈 Dicas para a Dissertação

### Análises Recomendadas

1. **Análise Descritiva**
   - Total de alunos, simulações, mensagens
   - Distribuição por persona
   - Taxa de conclusão por sessão

2. **Análise Temporal**
   - Horários de maior uso
   - Tempo médio por sessão
   - Padrões de abandono

3. **Análise de Conteúdo**
   - Comprimento médio de mensagens
   - Palavras-chave mais frequentes
   - Análise de sentimento (se aplicável)

4. **Análise Comparativa**
   - Diferenças entre personas
   - Evolução por sessão
   - Comparação entre alunos

### Gráficos Úteis

- Taxa de conclusão por sessão (gráfico de linha)
- Distribuição de mensagens por persona (gráfico de barras)
- Comprimento médio de respostas por sessão (box plot)
- Heatmap de uso por hora/dia da semana

---

## ❓ Dúvidas?

Se precisar de queries específicas para análises particulares da sua dissertação, é só pedir!
