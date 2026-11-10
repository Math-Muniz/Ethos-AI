# Ethos AI

Plataforma de pacientes simulados por modelo de linguagem para o treinamento de estudantes de Psicologia no tratamento do transtorno por uso de álcool. O estudante conduz, com um paciente simulado, as sete sessões centrais do manual de terapia de habilidades de enfrentamento (*Coping Skills Training*) do Project MATCH. Ao fim de cada sessão, um agente avaliador devolve um parecer de adesão ao protocolo, item a item, com justificativa apoiada na transcrição.

O sistema foi desenvolvido como parte da dissertação de mestrado *Ethos AI: Agentes de Inteligência Artificial Baseados em LLM como Pacientes Simulados para o Treinamento de Psicólogos no Tratamento do Alcoolismo*, do Programa de Pós-Graduação em Modelagem Computacional da Universidade Federal de Juiz de Fora (UFJF), 2026.

> **Aviso.** O Ethos AI é uma ferramenta de treinamento. Ele não oferece atendimento a pessoas com problemas relacionados ao álcool, e o parecer do agente avaliador é um apoio à autoavaliação do estudante, não uma medida de competência clínica.

## Personas

As três personas são fictícias. Os nomes, as histórias e as pessoas citadas por elas foram criados para o sistema e não têm relação com pessoas reais.

| Persona | Idade | AUDIT | Zona de risco | Estágio de mudança | Objetivo declarado |
|---|---|---|---|---|---|
| Clara | 35 | 19 | Uso nocivo | Preparação | Abstinência total |
| Luiz | 41 | 14 | Uso de risco | Contemplação | Reduzir, não parar |
| Rafael | 22 | 22 | Alto risco | Contemplação | Indeciso |

O sistema apresenta as personas sempre na ordem Clara, Luiz e Rafael. O botão **Novo paciente** abre uma conversa com a persona seguinte e volta para Clara depois de Rafael.

A especificação de Luiz é mais elaborada que a das outras duas. Ela inclui um elenco fixo de personagens, um bloco narrativo para cada sessão e memórias dos eventos das sessões já vividas, que entram no *prompt* de sistema conforme a sessão avança. O plano dessa reformulação está em `refinedplan.md`.

## Como funciona

- **Interface:** [Streamlit](https://streamlit.io/), com barra lateral de controle (relógio, estado da sessão, histórico de conversas, novo paciente, exportação da conversa e encerramento da sessão) e área principal de diálogo.
- **Orquestração:** [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview). O grafo tem um nó do paciente e sete nós de avaliação, um por sessão. A cada mensagem, uma função de roteamento verifica se ela contém o código de encerramento de sessão. Se contiver, a execução segue para o nó de avaliação da sessão corrente. Se não contiver, segue para o nó do paciente.
- **Modelo:** GPT-4o mini, acessado pela API da OpenAI, com temperatura zero. O modelo da persona e o do avaliador são definidos separadamente por variáveis de ambiente.
- **Contexto:** antes de cada chamada, o sistema conta os *tokens*. Se o total passar de 115.200 (90% da janela de 128 mil *tokens* do modelo), as mensagens mais antigas deixam de ser enviadas ao modelo. O *prompt* de sistema é mantido, e as mensagens continuam gravadas no banco.
- **Persistência:** PostgreSQL. O estado do grafo é salvo a cada passo pelo *checkpointer* do LangGraph, o que permite interromper e retomar uma conversa. A aplicação mantém ainda duas tabelas próprias, `session_metadata` e `conversation_log`, criadas automaticamente na primeira execução.
- **Observabilidade:** rastreamento opcional com [LangSmith](https://www.langchain.com/langsmith).

## Estrutura do repositório

| Arquivo | Conteúdo |
|---|---|
| `app.py` | Aplicação completa (interface, grafo, persistência, controle de acesso) |
| `prompts.py` | *Prompts* das três personas, blocos e memórias por sessão de Luiz e os sete *checklists* de avaliação |
| `requirements.txt` | Dependências com versões fixadas |
| `render.yaml` | Configuração de implantação no Render |
| `.env.example` | Modelo das variáveis de ambiente |
| `.streamlit/` | Configuração do Streamlit |
| `refinedplan.md` | Plano de reformulação da persona Luiz |
| `LICENSE` | Licença do código |

## Como executar localmente

Requisitos: Python 3.11, um banco PostgreSQL acessível e uma chave da API da OpenAI.

```bash
git clone https://github.com/Math-Muniz/Ethos-AI.git
cd Ethos-AI
python -m venv .venv
source .venv/bin/activate        # no Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # preencha as variáveis
streamlit run app.py
```

### Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `OPENAI_API_KEY` | Chave da API da OpenAI |
| `OPENAI_MODEL` | Modelo da persona (padrão `gpt-4o-mini`) |
| `OPENAI_MODEL_EVALUATOR` | Modelo do agente avaliador (padrão `gpt-4o-mini`) |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB` | Conexão com o PostgreSQL |
| `ALLOWED_USER_IDS` | Identificadores autorizados, separados por vírgula. Sem essa variável, a aplicação não inicia |
| `LANGSMITH_TRACING` | `true` para ativar o rastreamento (opcional) |
| `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | Configuração do LangSmith (opcional) |

### Acesso

O acesso é restrito aos identificadores listados em `ALLOWED_USER_IDS`. Cada participante recebe um *link* individual no formato

```
https://<endereço-da-aplicação>/?user=<identificador>
```

Para gerar um identificador:

```bash
python -c "import uuid; print(uuid.uuid4())"
```

Quem acessa sem um identificador autorizado vê uma página de acesso negado. As conversas ficam associadas ao identificador, de modo que o participante encontra o próprio histórico ao voltar pelo mesmo *link*.

## Implantação

O arquivo `render.yaml` publica a aplicação no [Render](https://render.com/). Ele instala as dependências, inicia o Streamlit e declara a verificação de integridade em `/?health=check`. A aplicação não depende do Render, já que é um serviço em Python iniciado por um único comando (`streamlit run app.py`), e pode ser publicada em outro provedor com configuração equivalente.

## Dados

Este repositório não contém dados de participantes. As conversas ficam gravadas no banco configurado em cada instalação. Os registros do estudo descrito na dissertação contêm relatos produzidos por participantes identificáveis e, por isso, não são disponibilizados.

## Base clínica

- KADDEN, R. et al. *Cognitive-Behavioral Coping Skills Therapy Manual: A Clinical Research Guide for Therapists Treating Individuals With Alcohol Abuse and Dependence*. Project MATCH Monograph Series, v. 3. Rockville, MD: National Institute on Alcohol Abuse and Alcoholism, 1995.
- MONTI, P. M. et al. *Treating Alcohol Dependence: A Coping Skills Training Guide*. New York: Guilford Press, 1989.

## Como citar

DAMASCO, M. M. **Ethos AI: Agentes de Inteligência Artificial Baseados em LLM como Pacientes Simulados para o Treinamento de Psicólogos no Tratamento do Alcoolismo**. 2026. Dissertação (Mestrado em Modelagem Computacional) – Faculdade de Engenharia, Universidade Federal de Juiz de Fora, Juiz de Fora, 2026.

## Licença

Ver o arquivo [LICENSE](LICENSE).
