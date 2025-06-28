# WYRD_RASP_BAXTER

Um servidor de monitoramento rodando em um Raspberry Pi, construído com FastAPI, para gerenciar e visualizar o estado de leitos e dispositivos embarcados em uma rede local.

## ✨ Funcionalidades Principais

* **Interface Web:** Painel para visualização e gerenciamento de dispositivos.
* **Gerenciamento de Leitos:** Cadastro, edição e listagem de leitos monitorados.
* **Gerenciamento de Embarcados:** Cadastro e listagem de dispositivos IoT/embarcados.
* **Visualização de Eventos:** Log de eventos importantes que ocorrem no sistema.
* **Escaneamento de Rede:** Módulo para identificar dispositivos na rede local (usando Nmap).
* **Servidor TCP:** Para comunicação direta com os dispositivos embarcados.
* **API RESTful:** Endpoints para interagir com o sistema de forma programática.

## 🛠️ Tecnologias Utilizadas

* **Backend:** Python 3, FastAPI
* **Servidor ASGI:** Uvicorn
* **Frontend:** HTML5, CSS3, Jinja2
* **Banco de Dados:** SQLite (inferido pelo arquivo `beds.db`)
* **Utilitários:** Nmap (para escaneamento de rede)

## 📂 Estrutura do Projeto

```
WYRD_RASP_BAXTER/
├── .venv/                  # Ambiente virtual Python
├── server_rasp/            # Código fonte principal do servidor
│   ├── app/                # Módulo da aplicação web (rotas, templates)
│   │   ├── web/
│   │   │   ├── static/     # Arquivos estáticos (CSS, JS, Imagens)
│   │   │   └── templates/  # Templates HTML (Jinja2)
│   │   └── __init__.py
│   ├── aggregator.py       # (Descreva a função deste arquivo)
│   ├── auth.py             # Lógica de autenticação
│   ├── config.py           # Configurações do projeto
│   ├── dispatcher.py       # (Descreva a função deste arquivo)
│   ├── main.py             # Ponto de entrada da aplicação FastAPI
│   ├── models.py           # Modelos de dados (ex: SQLAlchemy)
│   ├── nmap_scan.py        # Lógica para o escaneamento com Nmap
│   ├── presence.py         # Lógica de detecção de presença
│   └── tcp_server.py       # Implementação do servidor TCP
├── beds.db                 # Banco de dados SQLite
├── README.md               # Este arquivo
└── requirements.txt        # Dependências do projeto
```

## 🚀 Instalação e Execução

Siga os passos abaixo para configurar e rodar o ambiente de desenvolvimento.

**1. Clone o repositório:**
```bash
git clone [URL_DO_SEU_REPOSITORIO]
cd WYRD_RASP_BAXTER
```

**2. Crie e ative o ambiente virtual:**
```bash
# Criar o venv (só na primeira vez)
python -m venv .venv

# Ativar o venv (sempre que for trabalhar no projeto)
# Windows
.\.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

**3. Instale as dependências:**
```bash
pip install -r requirements.txt
```

**4. Configure as variáveis de ambiente:**
O projeto utiliza um arquivo `.env` para configurações. Crie uma cópia do arquivo `.env.example` (se você tiver um, é uma boa prática criá-lo) e o renomeie para `.env`.
```bash
# Exemplo de como poderia ser o conteúdo do .env
DATABASE_URL="sqlite:///./beds.db"
SECRET_KEY="uma_chave_secreta_muito_forte"
```

**5. Rode a aplicação:**
O ponto de entrada é o `server_rasp/main.py`. Para rodar o servidor FastAPI em modo de desenvolvimento:
```bash
uvicorn server_rasp.main:app --reload
```
* `--reload`: faz o servidor reiniciar automaticamente após qualquer alteração no código.

**6. Acesse a aplicação:**
Abra seu navegador e acesse [http://127.0.0.1:8000](http://127.0.0.1:8000).

---
## 📝 Endpoints da API

A documentação interativa da API (gerada automaticamente pelo FastAPI) está disponível em:
* **Swagger UI:** [/docs](http://127.0.0.1:8000/docs)
* **ReDoc:** [/redoc](http://127.0.0.1:8000/redoc)

---

## Licença
Distribuído sob a licença MIT. Veja `LICENSE` para mais informações.