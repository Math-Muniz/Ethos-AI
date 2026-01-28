import os
import streamlit as st
import streamlit.components.v1 as components
import uuid
import logging
import secrets
from typing import List, Annotated, TypedDict, Dict, Optional
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.messages.utils import trim_messages
from langgraph.graph import StateGraph, add_messages, END, START
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
try:
    import orjson
    USE_ORJSON = True
except ImportError:
    import json
    USE_ORJSON = False
from prompts import (
    PERSONA_RAFAEL, 
    PERSONA_CLARA, 
    PERSONA_LUIZ, 
    EVALUATION_SESSION_1, 
    EVALUATION_SESSION_2,
    EVALUATION_SESSION_3,
    EVALUATION_SESSION_4,
    EVALUATION_SESSION_5,
    EVALUATION_SESSION_6,
    EVALUATION_SESSION_7
)

# --- CONFIGURAÇÃO INICIAL E LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
st.set_page_config(page_title="ETHOS AI", page_icon="⚕️")
load_dotenv()

# --- CONFIGURAÇÃO LANGSMITH ---
if os.getenv("LANGSMITH_TRACING", "false").lower() == "true":
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = os.getenv("LANGSMITH_PROJECT", "ethos-ai")
    os.environ["LANGSMITH_ENDPOINT"] = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    os.environ["LANGSMITH_API_KEY"] = os.getenv("LANGSMITH_API_KEY")
    logger.info("✅ Langsmith tracing habilitado - Projeto: %s", os.getenv("LANGSMITH_PROJECT"))

# --- HEALTH CHECK ENDPOINT ---
if st.query_params.get("health") == "check":
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "ETHOS AI",
        "version": "1.0.0"
    }
    
    # Verificar LangSmith
    health_status["langsmith"] = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    
    # Verificar variáveis Open AI
    health_status["openai"] = bool(os.getenv("OPENAI_API_KEY"))
    health_status["openai_model_pacient"] = os.getenv("OPENAI_MODEL")
    health_status["openai_model_evaluator"] = os.getenv("OPENAI_MODEL_EVALUATOR")
    
    st.json(health_status)
    st.stop()

# --- CONSTANTES E VALIDAÇÕES INICIAIS ---
NUM_SESSIONS = 7  # Número total de sessões terapêuticas
MAX_CONTEXT_TOKENS = 115_200  # ~90% de 128K — margem de segurança para o modelo
END_SESSION_CODE = "H7Y4K9P2R1T6X3Z0V8B5N7M3G"
EVALUATION_METADATA_KEY = "is_evaluation"
BRAZIL_TZ = timezone(timedelta(hours=-3))
CLOCK_HTML = """<style> .digital-clock { background-color: #e1e5eb; border: 2px solid #c9ced4; border-radius: 5px; padding: 8px; font-family: sans-serif; color: #0d1a33; font-size: 1.75rem; font-weight: bold; text-align: center; letter-spacing: 2px; } </style><script> function updateClock() { var now = new Date(); var h = now.getHours().toString().padStart(2, '0'); var m = now.getMinutes().toString().padStart(2, '0'); var s = now.getSeconds().toString().padStart(2, '0'); document.getElementById('clock').innerText = h + ':' + m + ':' + s; } setInterval(updateClock, 1000); setTimeout(updateClock, 1); </script><div id="clock" class="digital-clock"></div>"""

# --- WHITELIST DE USUÁRIOS AUTORIZADOS ---
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = set(uid.strip() for uid in ALLOWED_USER_IDS_RAW.split(",") if uid.strip())

if not ALLOWED_USER_IDS:
    st.error("⚠️ ALLOWED_USER_IDS não configurado no arquivo .env! Aplicação bloqueada.")
    st.stop()

logger.info(f"✅ Whitelist carregada com {len(ALLOWED_USER_IDS)} usuários autorizados")

# Validação de API Keys
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
model_evaluator = os.getenv("OPENAI_MODEL_EVALUATOR", "gpt-4o-mini")

if not api_key:
    st.error("⚠️ OPENAI_API_KEY não encontrada! Configure no ambiente.")
    st.stop()

if not model or not model_evaluator:
    st.error("⚠️ OPENAI_MODEL ou OPENAI_MODEL_EVALUATOR não encontrados! Configure no ambiente.")
    st.stop()

logger.info(f"✅ Modelos configurados: Patient={model}, Evaluator={model_evaluator}")

# --- 3. ESTRUTURAS DE DADOS E DEFINIÇÕES GLOBAIS ---
PERSONAS_DATA = [
    {"name": "Clara", "prompt": PERSONA_CLARA, "order": 1},
    {"name": "Luiz", "prompt": PERSONA_LUIZ, "order": 2},
    {"name": "Rafael", "prompt": PERSONA_RAFAEL, "order": 3},
]

# Dicionários de lookup O(1) para personas
PERSONAS_BY_NAME = {p["name"]: p for p in PERSONAS_DATA}
PERSONAS_BY_ORDER = {p["order"]: p for p in PERSONAS_DATA}

EVALUATION_PROMPTS = {
    1: EVALUATION_SESSION_1,
    2: EVALUATION_SESSION_2,
    3: EVALUATION_SESSION_3,
    4: EVALUATION_SESSION_4,
    5: EVALUATION_SESSION_5,
    6: EVALUATION_SESSION_6,
    7: EVALUATION_SESSION_7
}

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    current_session: int
    session_end_indices: Dict[int, int]
    patient_prompt: str
    persona_name: str

# --- 4. FUNÇÕES HELPER GLOBAIS ---
def is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False

def filter_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    return [msg for msg in messages if not (isinstance(msg, HumanMessage) and END_SESSION_CODE in msg.content) and not (isinstance(msg, AIMessage) and msg.response_metadata.get(EVALUATION_METADATA_KEY))]

def create_transcript(messages: List[BaseMessage]) -> str:
    return "\n".join([f"{'Terapeuta' if isinstance(msg, HumanMessage) else 'Paciente'}: {msg.content}" for msg in messages])

def get_session_messages(state: AgentState, session_number: int) -> List[BaseMessage]:
    session_end_indices = state.get("session_end_indices", {})
    all_messages = state["messages"]
    
    if session_number == 1:
        start_idx = 0
    else:
        start_idx = session_end_indices.get(session_number - 1, 0)
    
    end_idx = len(all_messages)
    return all_messages[start_idx:end_idx]

def route_entry_point(state: AgentState) -> str:
    last_message = state["messages"][-1] if state["messages"] else None

    if isinstance(last_message, HumanMessage) and END_SESSION_CODE in last_message.content:
        current_session = state.get("current_session", 1)
        if 1 <= current_session <= NUM_SESSIONS:
            return f"evaluate_session_{current_session}"

    return "patient_node"

def get_next_persona(current_persona_name: str) -> Dict:
    current_persona = PERSONAS_BY_NAME.get(current_persona_name)

    if not current_persona:
        return PERSONAS_DATA[0]

    current_order = current_persona["order"]
    next_persona = PERSONAS_BY_ORDER.get(current_order + 1)

    return next_persona if next_persona else PERSONAS_DATA[0]

# --- 5. CONEXÃO SUPABASE (IPv4 Pooler) COM CONNECTION POOLING ---

def _get_connection_string() -> str:
    """Retorna a string de conexão para o PostgreSQL."""
    return (
        f"user={os.getenv('POSTGRES_USER')} "
        f"password={os.getenv('POSTGRES_PASSWORD')} "
        f"host={os.getenv('POSTGRES_HOST')} "
        f"port={os.getenv('POSTGRES_PORT')} "
        f"dbname={os.getenv('POSTGRES_DB')} "
        f"connect_timeout=10"
    )

# Pool de conexões global - reutiliza conexões em vez de criar novas
_db_pool: Optional[ConnectionPool] = None

def get_db_pool() -> ConnectionPool:
    """Retorna o pool de conexões, criando-o se necessário."""
    global _db_pool
    if _db_pool is None:
        _db_pool = ConnectionPool(
            conninfo=_get_connection_string(),
            min_size=2,
            max_size=10,
            check=ConnectionPool.check_connection,
            kwargs={
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5
            }
        )
        logger.info("✅ Connection pool criado (min=2, max=10, check=enabled)")
    return _db_pool

def _reset_db_pool():
    """Fecha e reseta o pool de conexões para forçar reconexão."""
    global _db_pool
    if _db_pool is not None:
        try:
            _db_pool.close()
        except Exception:
            pass
        _db_pool = None
        logger.info("🔄 Connection pool resetado")

def create_supabase_connection():
    """Cria conexão direta (para checkpointer e operações que precisam de conexão dedicada)."""
    return psycopg.connect(
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST"),
        port=os.getenv("POSTGRES_PORT"),
        dbname=os.getenv("POSTGRES_DB"),
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5
    )

def execute_db_query(query: str, params: tuple = None, fetch: bool = False):
    """Executa query usando connection pool com retry automático."""
    import time
    max_retries = 3
    retry_delay = 1
    pool = get_db_pool()

    for attempt in range(max_retries):
        try:
            with pool.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(query, params or ())

                    if fetch:
                        results = cur.fetchall()
                        conn.commit()
                        return results
                    else:
                        conn.commit()
                        return None

        except Exception as e:
            logger.warning(f"Tentativa {attempt + 1}/{max_retries} falhou: {e}")

            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2
                # Se erro de conexão, resetar o pool para forçar novas conexões
                error_msg = str(e).lower()
                if any(term in error_msg for term in [
                    "connection is closed", "connection was closed",
                    "broken pipe", "connection reset", "server closed",
                ]):
                    logger.warning("🔄 Erro de conexão detectado, resetando pool...")
                    _reset_db_pool()
                    pool = get_db_pool()
            else:
                logger.error(f"Erro após {max_retries} tentativas: {e}")
                raise
                    
def validate_db_connection():
    """Valida se a conexão com o banco está funcionando."""
    try:
        execute_db_query("SELECT 1", fetch=True)
        logger.info("✅ Conexão com banco de dados validada")
        return True
    except Exception as e:
        logger.error(f"❌ Conexão com banco falhou: {e}")
        return False

def setup_database():
    """Configura tabelas no Supabase."""
    logger.info("Configurando tabelas no Supabase...")
    
    queries = [
        # Criar tabela base
        """
        CREATE TABLE IF NOT EXISTS session_metadata (
            thread_id TEXT PRIMARY KEY,
            persona_name TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'legacy',
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            last_accessed TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """,
        
        # Adicionar coluna session_count se não existir
        """
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='session_metadata' AND column_name='session_count'
            ) THEN
                ALTER TABLE session_metadata ADD COLUMN session_count INTEGER DEFAULT 0;
            END IF;
        END $$;
        """,
        
        # Adicionar coluna total_messages se não existir
        """
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='session_metadata' AND column_name='total_messages'
            ) THEN
                ALTER TABLE session_metadata ADD COLUMN total_messages INTEGER DEFAULT 0;
            END IF;
        END $$;
        """,
        
        # ✅ EDITADO: Adicionar coluna session_complete se não existir
        """
        DO $$ 
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='session_metadata' AND column_name='session_complete'
            ) THEN
                ALTER TABLE session_metadata ADD COLUMN session_complete BOOLEAN DEFAULT FALSE;
            END IF;
        END $$;
        """,
        
        # Índices
        """
        CREATE INDEX IF NOT EXISTS idx_user_sessions
        ON session_metadata(user_id, created_at DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_last_accessed
        ON session_metadata(last_accessed)
        """,

        # Criar tabela conversation_log para análise de dissertação
        """
        CREATE TABLE IF NOT EXISTS conversation_log (
            id SERIAL PRIMARY KEY,
            thread_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            persona_name TEXT NOT NULL,
            session_number INTEGER NOT NULL,
            message_type TEXT NOT NULL,
            message_content TEXT NOT NULL,
            message_order INTEGER NOT NULL,
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            metadata JSONB
        )
        """,

        # Índices para conversation_log
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_thread
        ON conversation_log(thread_id, message_order)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_conversation_user
        ON conversation_log(user_id, timestamp DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_conversation_persona
        ON conversation_log(persona_name, session_number)
        """
    ]
    
    for query in queries:
        try:
            execute_db_query(query)
        except Exception as e:
            logger.error(f"Erro ao executar query de setup: {e}")
    
    logger.info("✅ Database configurado com sucesso")

# --- 6. FUNÇÕES CACHEADAS ---

@st.cache_resource
def get_llms():
    logger.info("Criando instâncias dos LLMs...")
    try:
        patient_llm = ChatOpenAI(
            model=model,
            openai_api_key=api_key,
            temperature=0,
            max_retries=3,
            timeout=30
        )
        
        evaluator_llm = ChatOpenAI(
            model=model_evaluator,
            openai_api_key=api_key,
            temperature=0,
            max_retries=3,
            timeout=90
        )
        
        logger.info("✅ LLMs inicializados com sucesso")
        return patient_llm, evaluator_llm
    except Exception as e:
        st.error(f"❌ Erro ao inicializar modelos: {e}")
        st.stop()

# Gerenciamento global da conexão do checkpointer com reconexão automática
_checkpointer_conn = None
_checkpointer: Optional[PostgresSaver] = None

def ensure_checkpointer_connection() -> PostgresSaver:
    """Garante que a conexão do checkpointer está viva, reconectando se necessário."""
    global _checkpointer_conn, _checkpointer

    needs_reconnect = False

    if _checkpointer_conn is None or _checkpointer_conn.closed:
        needs_reconnect = True
    else:
        try:
            _checkpointer_conn.execute("SELECT 1")
        except Exception:
            needs_reconnect = True

    if needs_reconnect:
        logger.warning("🔄 Reconectando checkpointer ao banco de dados...")
        try:
            if _checkpointer_conn and not _checkpointer_conn.closed:
                _checkpointer_conn.close()
        except Exception:
            pass

        _checkpointer_conn = create_supabase_connection()

        if _checkpointer is None:
            _checkpointer = PostgresSaver(conn=_checkpointer_conn)
            _checkpointer_conn.autocommit = True
            _checkpointer.setup()
            _checkpointer_conn.autocommit = False
        else:
            _checkpointer.conn = _checkpointer_conn

        logger.info("✅ Checkpointer reconectado com sucesso")

    return _checkpointer

@st.cache_resource
def get_app_and_checkpointer(_patient_llm, _evaluator_llm):
    logger.info("Compilando grafo LangGraph...")

    # Conexão dedicada para checkpointer com reconexão automática
    checkpointer = ensure_checkpointer_connection()
    
    def patient_node(state: AgentState) -> Dict:
        system_prompt = SystemMessage(content=state["patient_prompt"])
        filtered = filter_messages(state["messages"])
        messages_to_send = [system_prompt] + filtered
        token_count = _patient_llm.get_num_tokens_from_messages(messages_to_send)
        if token_count > MAX_CONTEXT_TOKENS:
            logger.warning(
                f"⚠️ patient_node: {token_count} tokens excede limite de {MAX_CONTEXT_TOKENS}. Trimando contexto..."
            )
            messages_to_send = trim_messages(
                messages_to_send,
                max_tokens=MAX_CONTEXT_TOKENS,
                strategy="last",
                token_counter=_patient_llm,
                include_system=True,
                start_on="human",
            )
        response = _patient_llm.invoke(messages_to_send)
        return {"messages": [response]}
    
    def create_evaluation_node(session_number: int):
        def evaluation_node(state: AgentState) -> Dict:
            session_messages = get_session_messages(state, session_number)
            filtered_session = filter_messages(session_messages)
            evaluation_prompt = EVALUATION_PROMPTS[session_number]
            prompt_with_transcript = evaluation_prompt.format(
                transcript=create_transcript(filtered_session)
            )
            prompt_messages = [HumanMessage(content=prompt_with_transcript)]
            token_count = _evaluator_llm.get_num_tokens_from_messages(prompt_messages)
            if token_count > MAX_CONTEXT_TOKENS:
                logger.warning(
                    f"⚠️ evaluation_node (sessão {session_number}): "
                    f"{token_count} tokens excede limite de {MAX_CONTEXT_TOKENS}. Trimando transcript..."
                )
                trimmed_session = trim_messages(
                    filtered_session,
                    max_tokens=MAX_CONTEXT_TOKENS,
                    strategy="last",
                    token_counter=_evaluator_llm,
                    start_on="human",
                )
                prompt_with_transcript = evaluation_prompt.format(
                    transcript=create_transcript(trimmed_session)
                )
            response = _evaluator_llm.invoke(prompt_with_transcript)
            
            session_end_indices = state.get("session_end_indices", {}).copy()
            session_end_indices[session_number] = len(state["messages"]) + 1
            
            return {
                "messages": [AIMessage(content=response.content, response_metadata={EVALUATION_METADATA_KEY: True})],
                "current_session": session_number + 1,
                "session_end_indices": session_end_indices
            }
        
        return evaluation_node
    
    workflow = StateGraph(AgentState)
    workflow.add_node("patient_node", patient_node)
    
    for i in range(1, NUM_SESSIONS + 1):
        workflow.add_node(f"evaluation_{i}_node", create_evaluation_node(i))
    
    workflow.add_conditional_edges(
        START,
        route_entry_point,
        {
            "patient_node": "patient_node",
            "evaluate_session_1": "evaluation_1_node",
            "evaluate_session_2": "evaluation_2_node",
            "evaluate_session_3": "evaluation_3_node",
            "evaluate_session_4": "evaluation_4_node",
            "evaluate_session_5": "evaluation_5_node",
            "evaluate_session_6": "evaluation_6_node",
            "evaluate_session_7": "evaluation_7_node"
        }
    )
    
    workflow.add_edge("patient_node", END)
    for i in range(1, NUM_SESSIONS + 1):
        workflow.add_edge(f"evaluation_{i}_node", END)

    app = workflow.compile(checkpointer=checkpointer)
    logger.info(f"✅ Aplicação LangGraph compilada com {NUM_SESSIONS} sessões")
    return app, checkpointer

def safe_invoke(app, invoke_args, invoke_config, max_retries=2):
    """Executa app.invoke() com retry e reconexão automática do checkpointer."""
    import time
    for attempt in range(max_retries):
        try:
            ensure_checkpointer_connection()
            return app.invoke(invoke_args, invoke_config)
        except Exception as e:
            error_msg = str(e).lower()
            is_connection_error = any(term in error_msg for term in [
                "connection is closed", "connection was closed",
                "server closed the connection", "broken pipe",
                "connection reset", "connection refused",
                "connection timed out", "operationalerror",
            ])
            if is_connection_error and attempt < max_retries - 1:
                logger.warning(
                    f"🔄 Erro de conexão no invoke (tentativa {attempt + 1}/{max_retries}): {e}. Reconectando..."
                )
                time.sleep(2 ** attempt)
                # Forçar reconexão na próxima iteração
                global _checkpointer_conn
                try:
                    if _checkpointer_conn and not _checkpointer_conn.closed:
                        _checkpointer_conn.close()
                except Exception:
                    pass
                _checkpointer_conn = None
            else:
                raise

# --- 7. FUNÇÕES DE MÉTRICAS ---

# ✅ EDITADO: Função update_session_stats agora inclui session_complete
def update_session_stats(thread_id: str, session_num: int, total_msgs: int):
    """Atualiza estatísticas consolidadas da sessão."""
    # Determina se a sessão está completa (chegou à última sessão ou superior)
    is_complete = session_num >= NUM_SESSIONS
    
    query = """
        UPDATE session_metadata 
        SET session_count = %s, 
            total_messages = %s,
            session_complete = %s,
            last_accessed = CURRENT_TIMESTAMP
        WHERE thread_id = %s
    """
    try:
        execute_db_query(query, (session_num, total_msgs, is_complete, thread_id))
        status = "COMPLETA" if is_complete else "EM ANDAMENTO"
        logger.info(f"✅ Stats atualizados: thread={thread_id}, session={session_num}, msgs={total_msgs}, status={status}")
    except Exception as e:
        logger.warning(f"Erro ao atualizar stats: {e}")

def _serialize_json(data: Dict) -> Optional[str]:
    """Serializa dados para JSON usando orjson se disponível (3-5x mais rápido)."""
    if data is None:
        return None
    if USE_ORJSON:
        return orjson.dumps(data).decode('utf-8')
    else:
        import json
        return json.dumps(data)

def log_conversation_message(
    thread_id: str,
    user_id: str,
    persona_name: str,
    session_number: int,
    message_type: str,
    message_content: str,
    message_order: int,
    metadata: Dict = None
):
    """Salva mensagem individual na tabela conversation_log para análise."""
    query = """
        INSERT INTO conversation_log
        (thread_id, user_id, persona_name, session_number, message_type,
         message_content, message_order, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        metadata_json = _serialize_json(metadata)
        execute_db_query(query, (
            thread_id, user_id, persona_name, session_number,
            message_type, message_content, message_order, metadata_json
        ))
    except Exception as e:
        logger.warning(f"Erro ao logar mensagem: {e}")

# --- 8. INICIALIZAÇÃO ---
setup_database()

if not validate_db_connection():
    st.error("❌ Não foi possível conectar ao banco de dados. Verifique as configurações.")
    st.stop()

patient_llm, evaluator_llm = get_llms()
app, checkpointer = get_app_and_checkpointer(patient_llm, evaluator_llm)

# --- 9. GERENCIAMENTO DE USER_ID ---

def is_user_authorized(user_id: str) -> bool:
    is_authorized = user_id in ALLOWED_USER_IDS
    if not is_authorized:
        logger.warning(f"🚫 Tentativa de acesso não autorizado: {user_id}")
    return is_authorized

def get_or_create_user_id():
    url_user_id = st.query_params.get("user")
    
    if url_user_id and "user_id" not in st.session_state:
        st.session_state.user_id = url_user_id
        logger.info(f"User ID recuperado da URL: {url_user_id}")
    
    if "user_id" not in st.session_state:
        new_user_id = secrets.token_urlsafe(16)
        st.session_state.user_id = new_user_id
        st.query_params.user = new_user_id
        logger.info(f"Novo User ID criado: {new_user_id}")
    
    elif st.query_params.get("user") != st.session_state.user_id:
        st.query_params.user = st.session_state.user_id
    
    return st.session_state.user_id

def show_unauthorized_page():
    st.error("# 🚫 Acesso Negado")
    st.markdown("""
    ### Você não tem permissão para acessar este aplicativo.
    
    Este é um sistema restrito para uso exclusivo de participantes autorizados do **Project Match**.
    
    #### Como obter acesso?
    Entre em contato com o administrador do projeto.
    """)
    
    with st.expander("ℹ️ Informações Técnicas"):
        st.code(f"User ID: {st.session_state.get('user_id', 'N/A')}")
    
    st.stop()

# --- 10. FUNÇÕES DE SESSÃO ---

# ✅ EDITADO: get_recent_sessions agora inclui session_complete no SELECT
def get_recent_sessions(limit: int = 50) -> List[Dict]:
    query = """
        SELECT 
            thread_id, 
            persona_name, 
            created_at, 
            last_accessed,
            session_count,
            total_messages,
            session_complete
        FROM session_metadata 
        WHERE user_id = %s
        ORDER BY created_at DESC 
        LIMIT %s
    """
    try:
        user_id = st.session_state.user_id
        results = execute_db_query(query, (user_id, limit), fetch=True)
        return results or []
    except Exception as e:
        logger.error(f"Erro ao buscar sessões: {e}")
        return []

def save_session_metadata(thread_id: str, persona_name: str):
    query = """
        INSERT INTO session_metadata (thread_id, persona_name, user_id, last_accessed)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (thread_id) DO UPDATE 
        SET persona_name = EXCLUDED.persona_name,
            last_accessed = CURRENT_TIMESTAMP,
            user_id = EXCLUDED.user_id
    """
    try:
        user_id = st.session_state.user_id
        execute_db_query(query, (thread_id, persona_name, user_id))
        logger.info(f"Metadados salvos: {thread_id} -> {persona_name}")
    except Exception as e:
        logger.error(f"Erro ao salvar metadados: {e}")

def load_session_metadata(thread_id: str) -> Optional[str]:
    # Query combinada: UPDATE + RETURNING evita duas round-trips ao banco
    query = """
        UPDATE session_metadata
        SET last_accessed = CURRENT_TIMESTAMP
        WHERE thread_id = %s AND user_id = %s
        RETURNING persona_name
    """
    try:
        user_id = st.session_state.user_id
        logger.info(f"Tentando carregar metadados: thread_id={thread_id}, user_id={user_id}")
        results = execute_db_query(query, (thread_id, user_id), fetch=True)

        if results and len(results) > 0:
            logger.info(f"✅ Metadados encontrados: persona={results[0]['persona_name']}")
            return results[0]['persona_name']
        else:
            logger.warning(f"❌ Nenhum metadado encontrado para thread_id={thread_id}, user_id={user_id}")
    except Exception as e:
        logger.error(f"Erro ao carregar metadados: {e}")

    return None

def load_session_from_checkpoint(thread_id: str) -> bool:
    try:
        logger.info(f"Carregando sessão: {thread_id}")

        persona_name = load_session_metadata(thread_id)
        if not persona_name:
            return False

        # Lookup O(1) usando dicionário em vez de busca linear
        persona_data = PERSONAS_BY_NAME.get(persona_name)
        if not persona_data:
            return False

        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

        # Garantir que a conexão do checkpointer está viva antes de consultar
        import time
        max_retries = 2
        saved_state = None
        for attempt in range(max_retries):
            try:
                ensure_checkpointer_connection()
                saved_state = checkpointer.get(config)
                break
            except Exception as e:
                error_msg = str(e).lower()
                is_connection_error = any(term in error_msg for term in [
                    "connection is closed", "connection was closed",
                    "broken pipe", "connection reset", "server closed",
                ])
                if is_connection_error and attempt < max_retries - 1:
                    logger.warning(f"🔄 Conexão perdida ao carregar checkpoint (tentativa {attempt + 1}): {e}")
                    time.sleep(1)
                    # Forçar reconexão
                    global _checkpointer_conn
                    try:
                        if _checkpointer_conn and not _checkpointer_conn.closed:
                            _checkpointer_conn.close()
                    except Exception:
                        pass
                    _checkpointer_conn = None
                else:
                    raise

        messages = []
        current_session = 1
        session_end_indices = {}
        
        if saved_state and saved_state.get("channel_values"):
            channel_values = saved_state["channel_values"]
            messages = channel_values.get("messages", [])
            current_session = channel_values.get("current_session", 1)
            session_end_indices = channel_values.get("session_end_indices", {})
        
        st.session_state.messages = messages
        st.session_state.current_session_num = current_session
        st.session_state.session_end_indices = session_end_indices
        st.session_state.thread_id = thread_id
        st.session_state.current_patient = persona_data
        
        logger.info(f"Sessão restaurada: {persona_name}, sessão {current_session}")
        if messages:
            st.toast(f"Sessão restaurada com {persona_name}!")
        return True
                    
    except Exception as e:
        logger.warning(f"Erro ao carregar sessão: {e}")
    
    return False

def initialize_session(thread_id: str = None, force_new: bool = False):
    logger.info(f"🔄 initialize_session chamado: thread_id={thread_id}, force_new={force_new}")

    # Validação básica de UUID se fornecido
    if thread_id and not is_valid_uuid(thread_id):
        logger.warning(f"Thread ID inválido: {thread_id}")
        st.warning("⚠️ Link inválido. Verificando histórico...")
        thread_id = None

    # 1. CENÁRIO: Link direto com thread_id (ex: clicou no histórico ou link compartilhado)
    if thread_id and not force_new:
        logger.info(f"Tentando carregar sessão existente: {thread_id}")
        if load_session_from_checkpoint(thread_id):
            logger.info(f"✅ Sessão carregada com sucesso!")
            return
        else:
            logger.warning(f"❌ Falha ao carregar sessão {thread_id}, criando nova...")

    # Cache de recent_sessions para evitar múltiplas queries na mesma inicialização
    recent_sessions = get_recent_sessions(limit=1)

    # 2. CENÁRIO (NOVO): Link limpo (raiz) e não forçou novo paciente -> Tenta recuperar o último
    if not thread_id and not force_new:
        if recent_sessions:
            last_thread_id = recent_sessions[0]['thread_id']
            logger.info(f"URL sem thread. Retomando a última sessão encontrada: {last_thread_id}")

            if load_session_from_checkpoint(last_thread_id):
                # Importante: Atualiza a URL para o usuário saber onde está
                st.query_params.thread_id = last_thread_id
                return

    # 3. CENÁRIO: Novo Paciente (force_new=True) OU Primeira vez (sem histórico)
    logger.info("Criando nova sessão (force_new=%s)", force_new)
    new_thread_id = str(uuid.uuid4())

    # Reutiliza recent_sessions já carregado (evita segunda query)
    if recent_sessions and len(recent_sessions) > 0:
        last_persona_name = recent_sessions[0]['persona_name']
        # Rotaciona para o próximo apenas se for forçado ou se for uma criação real
        new_patient = get_next_persona(last_persona_name)
        logger.info(f"Última: {last_persona_name}, próximo: {new_patient['name']}")
    else:
        new_patient = PERSONAS_DATA[0]
        logger.info(f"Primeira sessão: {new_patient['name']}")
    
    save_session_metadata(new_thread_id, new_patient['name'])
    
    st.session_state.messages = []
    st.session_state.thread_id = new_thread_id
    st.session_state.current_patient = new_patient
    st.session_state.current_session_num = 1
    st.session_state.session_end_indices = {}
    
    st.query_params.thread_id = new_thread_id
    logger.info(f"✅ Nova sessão criada: {new_thread_id}")
    st.toast(f"✅ Novo paciente: {new_patient['name']}!")

# --- 11. VALIDAÇÃO E INICIALIZAÇÃO ---

get_or_create_user_id()

if not is_user_authorized(st.session_state.user_id):
    show_unauthorized_page()

logger.info(f"✅ Usuário autorizado: {st.session_state.user_id}")

# Lógica simplificada graças ao Exemplo 1
# Prioridade 1: Se foi marcado para carregar uma sessão específica
if "_load_thread_id" in st.session_state:
    thread_to_load = st.session_state._load_thread_id
    del st.session_state._load_thread_id  # Limpar flag
    logger.info(f"Carregando sessão marcada: {thread_to_load}")
    initialize_session(thread_to_load)
else:
    url_thread_id = st.query_params.get("thread_id")
    current_thread_id = st.session_state.get("thread_id")

    if url_thread_id and url_thread_id != current_thread_id:
        # Se a URL mudou, recarrega
        initialize_session(url_thread_id)
    elif "thread_id" not in st.session_state:
        # Se não tem sessão carregada (mesmo que url_thread_id seja None), inicializa
        # A função initialize_session vai decidir se recupera o histórico ou cria novo
        initialize_session(url_thread_id)

st.markdown("<h1 style='text-align: center;'>ETHOS AI</h1>", unsafe_allow_html=True)

# --- 12. INTERFACE ---

with st.sidebar:
    st.title("Painel de Controle")
    components.html(CLOCK_HTML, height=65)
    
    st.header("Status da Simulação")
    if st.session_state.current_session_num <= NUM_SESSIONS:
        st.info(
            f"Sessão: **{st.session_state.current_session_num}/{NUM_SESSIONS}** | "
            f"Paciente: **{st.session_state.current_patient['name']}**",
            icon="⚠️"
        )
        progress = (st.session_state.current_session_num - 1) / NUM_SESSIONS
        st.progress(progress)
    else:
        st.success(f"✅ Todas as {NUM_SESSIONS} sessões concluídas!", icon="🎉")
    
    with st.expander("ℹ️ Informações de Acesso", expanded=False):
        st.caption(f"✅ Acesso Autorizado")
        st.caption(f"Thread ID: {st.session_state.thread_id}")
        st.caption(f"📅 Iniciada: {datetime.now(BRAZIL_TZ).strftime('%H:%M')}")
        
    st.header("Suas Conversas")
    recent_sessions = get_recent_sessions(limit=50)

    if recent_sessions:
        current_tid = st.session_state.get("thread_id")
        
        for session in recent_sessions:
            thread_id = session['thread_id']
            persona = session['persona_name']
            created_at = session['created_at']
            last_accessed = session['last_accessed']
            
            is_current = thread_id == current_tid
            
            now_utc = datetime.now(timezone.utc)
            last_accessed_utc = last_accessed if last_accessed.tzinfo else last_accessed.replace(tzinfo=timezone.utc)
            time_diff = now_utc - last_accessed_utc
            
            if time_diff.days > 0:
                time_str = f"{time_diff.days}d atrás"
            elif time_diff.seconds > 3600:
                time_str = f"{time_diff.seconds // 3600}h atrás"
            else:
                time_str = f"{time_diff.seconds // 60}min atrás"
            
            created_at_local = created_at.astimezone(BRAZIL_TZ)
            date_str = created_at_local.strftime('%d/%m %H:%M')
            
            button_label = f"{'🟢' if is_current else '⚪'} {persona} - {date_str}"

            if st.button(
                button_label,
                key=f"session_{thread_id}",
                use_container_width=True,
                disabled=is_current,
                help=time_str
            ):
                # Marcar para carregar sessão específica
                st.session_state._load_thread_id = thread_id

                # Limpar session state
                for key in ['messages', 'current_session_num', 'session_end_indices',
                        'thread_id', 'current_patient']:
                    if key in st.session_state:
                        del st.session_state[key]

                # Atualizar URL
                st.query_params.thread_id = thread_id
                st.rerun()
    else:
        st.caption("Nenhuma conversa anterior")
    
    st.header("Controles")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 Novo Paciente", use_container_width=True):
            initialize_session(force_new=True)
            st.rerun()
    with col2:
        def export_session_history():
            now_local = datetime.now(BRAZIL_TZ)
            output = [f"Paciente: {st.session_state.current_patient['name']}\nData: {now_local.strftime('%d/%m/%Y %H:%M:%S')}\n\n"]
            for msg in st.session_state.messages:
                if isinstance(msg, AIMessage) and msg.response_metadata.get(EVALUATION_METADATA_KEY):
                    output.append(f"\n--- AVALIAÇÃO ---\n{msg.content}\n-----------------\n")
                elif isinstance(msg, AIMessage):
                    output.append(f"Paciente: {msg.content}\n")
                elif isinstance(msg, HumanMessage) and END_SESSION_CODE not in msg.content:
                    output.append(f"Terapeuta: {msg.content}\n")
            return "".join(output)
        
        if st.session_state.messages:
            st.download_button(
                "💾 Download", 
                export_session_history(), 
                f"sessao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt", 
                "text/plain", 
                use_container_width=True
            )
        else:
            st.button("💾 Download", use_container_width=True, disabled=True)
    
    if st.session_state.current_session_num <= NUM_SESSIONS:
        if st.button("🏁 Encerrar Sessão e Avaliar", type="primary", use_container_width=True):
            with st.spinner("⏳ Gerando avaliação detalhada..."):
                try:
                    response = safe_invoke(
                        app,
                        {
                            "messages": st.session_state.messages + [HumanMessage(content=END_SESSION_CODE)],
                            "current_session": st.session_state.current_session_num,
                            "session_end_indices": st.session_state.get("session_end_indices", {}),
                            "patient_prompt": st.session_state.current_patient['prompt'],
                            "persona_name": st.session_state.current_patient['name']
                        },
                        {
                            "configurable": {"thread_id": st.session_state.thread_id},
                            "metadata": {
                                "user_id": st.session_state.user_id,
                                "persona": st.session_state.current_patient['name'],
                                "session_num": st.session_state.current_session_num,
                                "action": "evaluate_session"
                            }
                        }
                    )

                    evaluation_message = response["messages"][-1]
                    st.session_state.messages.append(evaluation_message)

                    # Logar avaliação
                    log_conversation_message(
                        thread_id=st.session_state.thread_id,
                        user_id=st.session_state.user_id,
                        persona_name=st.session_state.current_patient['name'],
                        session_number=st.session_state.current_session_num,
                        message_type='evaluation',
                        message_content=evaluation_message.content,
                        message_order=len(st.session_state.messages),
                        metadata={'completed_session': st.session_state.current_session_num}
                    )

                    if "current_session" in response:
                        new_session_num = response["current_session"]
                        st.session_state.current_session_num = new_session_num
                        
                        if "session_end_indices" in response:
                            st.session_state.session_end_indices = response["session_end_indices"]
                        
                        # Atualizar estatísticas consolidadas
                        update_session_stats(
                            st.session_state.thread_id,
                            new_session_num - 1,  # Sessão que acabou de ser avaliada
                            len(st.session_state.messages)
                        )
                        
                        if new_session_num <= NUM_SESSIONS:
                            st.toast(f"✅ Sessão {new_session_num - 1} avaliada! Iniciando Sessão {new_session_num}...")
                        else:
                            st.toast(f"🎉 Todas as {NUM_SESSIONS} sessões concluídas!")
                    
                    st.rerun()
                except TimeoutError:
                    st.error("⏱️ Tempo limite excedido ao gerar avaliação. Tente novamente.")
                except ConnectionError:
                    st.error("🔌 Erro de conexão. Verifique sua internet.")
                except Exception as e:
                    logger.error(f"Erro durante avaliação: {e}")
                    st.error(f"❌ Erro ao processar avaliação: {str(e)}")

# --- 13. RENDERIZAÇÃO DO CHAT ---

session_end_indices = st.session_state.get("session_end_indices", {})

# Dicionário reverso O(1) para lookup: índice -> número da sessão (evita loop O(n×7))
index_to_session = {(idx - 1): session_num for session_num, idx in session_end_indices.items()}

for i, msg in enumerate(st.session_state.messages):
    if isinstance(msg, AIMessage) and msg.response_metadata.get(EVALUATION_METADATA_KEY):
        with st.chat_message("assistant", avatar="📋"):
            st.markdown("### 📊 Avaliação da Sessão\n" + msg.content)
    elif isinstance(msg, AIMessage):
        st.chat_message("assistant", avatar="🧑‍⚕️").write(msg.content)
    elif isinstance(msg, HumanMessage) and END_SESSION_CODE not in msg.content:
        st.chat_message("user", avatar="👨‍💻").write(msg.content)

    # Lookup O(1) em vez de loop O(7)
    if i in index_to_session:
        session_num = index_to_session[i]
        st.divider()
        if session_num < NUM_SESSIONS:
            st.subheader(f"🔄 Sessão {session_num + 1}")
        else:
            st.subheader("✅ Fim da Simulação")
            st.info("💡 Use 'Download' para salvar ou 'Novo Paciente' para continuar.", icon="ℹ️")
        st.divider()

# --- 14. INPUT DO CHAT ---

if prompt := st.chat_input("Digite sua mensagem...", disabled=(st.session_state.current_session_num > NUM_SESSIONS)):
    if not prompt.strip():
        st.warning("⚠️ Por favor, digite uma mensagem válida.")
    else:
        st.session_state.messages.append(HumanMessage(content=prompt))

        # Logar mensagem do terapeuta
        log_conversation_message(
            thread_id=st.session_state.thread_id,
            user_id=st.session_state.user_id,
            persona_name=st.session_state.current_patient['name'],
            session_number=st.session_state.current_session_num,
            message_type='therapist',
            message_content=prompt,
            message_order=len(st.session_state.messages)
        )

        st.rerun()

if st.session_state.messages and isinstance(st.session_state.messages[-1], HumanMessage) and END_SESSION_CODE not in st.session_state.messages[-1].content:
    with st.chat_message("assistant", avatar="🧑‍⚕️"):
        with st.spinner("💭 Paciente Digitando..."):
            try:
                response = safe_invoke(
                    app,
                    {
                        "messages": st.session_state.messages,
                        "current_session": st.session_state.current_session_num,
                        "session_end_indices": st.session_state.get("session_end_indices", {}),
                        "patient_prompt": st.session_state.current_patient['prompt'],
                        "persona_name": st.session_state.current_patient['name']
                    },
                    {
                        "configurable": {"thread_id": st.session_state.thread_id},
                        "metadata": {
                            "user_id": st.session_state.user_id,
                            "persona": st.session_state.current_patient['name'],
                            "session_num": st.session_state.current_session_num,
                            "action": "patient_response"
                        }
                    }
                )
                ai_response = response["messages"][-1]
                st.session_state.messages.append(ai_response)

                # Logar resposta do paciente
                log_conversation_message(
                    thread_id=st.session_state.thread_id,
                    user_id=st.session_state.user_id,
                    persona_name=st.session_state.current_patient['name'],
                    session_number=st.session_state.current_session_num,
                    message_type='patient',
                    message_content=ai_response.content,
                    message_order=len(st.session_state.messages)
                )

                st.rerun()
            except TimeoutError:
                st.error("⏱️ Tempo limite excedido. Tente novamente.")
            except ConnectionError:
                st.error("🔌 Erro de conexão. Verifique sua internet.")
            except Exception as e:
                logger.error(f"Erro ao gerar resposta: {e}")
                st.error(f"❌ Erro ao gerar resposta: {str(e)}")
