🏥 Baxter - Sistema de Localização de Camas em Tempo Real
Um sistema de IoT para monitoramento em tempo real da localização de camas hospitalares, construído com FastAPI, MQTT e ESP32.

✨ Funcionalidades Principais
Localização em Tempo Real: Determina em qual quarto cada cama está, com base na intensidade do sinal de beacons Bluetooth.

Sincronização via MQTT: Utiliza um broker MQTT para enviar em tempo real a lista de camas disponíveis para todos os dispositivos, evitando conflitos e leituras duplicadas.

Interface Web de Gestão: Painel administrativo para cadastrar, editar e visualizar o estado de Camas e Dispositivos ESP.

Histórico de Eventos: Log completo de todos os eventos de GET e OUT recebidos, com informações de RSSI, WiFi, e data.

Validação de Presença na Rede: Confirma se o módulo Wi-Fi de uma cama está ativo na rede (nmap/arp) antes de associá-la a um quarto, aumentando a confiabilidade.

Lógica de Agregação Inteligente: Processa eventos de múltiplas fontes, elegendo o ESP com o sinal mais forte como a localização correta da cama.

🛠️ Tecnologias Utilizadas
Backend: Python 3, FastAPI

Servidor ASGI: Uvicorn

Banco de Dados: SQLite

Mensageria: MQTT (com o broker Mosquitto)

Hardware: ESP32, Beacons BLE

Frontend (Admin): HTML5, CSS3, Jinja2

Bibliotecas Python Notáveis: SQLAlchemy, Paho-MQTT, Nmap

📂 Estrutura do Projeto
WYRD_RASP_BAXTER/
├── .venv/                  # Ambiente virtual Python
├── server_rasp/            # Código fonte principal do servidor
│   ├── app/                # Módulo da aplicação
│   │   ├── web/
│   │   │   ├── static/     # Arquivos estáticos (CSS, JS, Imagens)
│   │   │   └── templates/  # Templates HTML (Jinja2)
│   │   ├── __init__.py
│   └── │   └── ...
│   ├── aggregator.py       # Cérebro do sistema. Processa eventos, resolve conflitos e atualiza o estado.
│   ├── auth.py             # Lógica de autenticação para as rotas de admin.
│   ├── config.py           # Configurações do projeto (IPs, portas, tópicos MQTT).
│   ├── dispatcher.py       # Envia o estado final para um sistema externo (servidor Connecta).
│   ├── main.py             # Ponto de entrada da aplicação FastAPI e rotas.
│   ├── models.py           # Modelos de dados do SQLAlchemy (Bed, Embarcado).
│   ├── nmap_scan.py        # Lógica para o escaneamento de rede.
│   ├── presence.py         # Lógica de verificação de presença de MAC na rede.
│   ├── tcp_server.py       # (Depreciado) Servidor TCP original.
│   └── mqtt_client.py  # Lógica do cliente MQTT para publicar atualizações
├── beds.db                 # Banco de dados SQLite
├── README.md               # Este arquivo
└── requirements.txt        # Dependências do projeto Python

🚀 Instalação e Execução
Siga os passos abaixo para configurar e rodar o ambiente completo.

1. Broker MQTT (Mosquitto)
Instale o Mosquitto no seu sistema operacional.

Edite o arquivo mosquitto.conf para permitir acesso pela rede (adicione as linhas abaixo):

Snippet de código

listener 1883 0.0.0.0
allow_anonymous true
Inicie o serviço do Mosquitto. No Windows, use net start mosquitto em um terminal de administrador.

2. Backend (Servidor FastAPI)
Clone o repositório:

Bash

git clone <URL_DO_SEU_REPOSITORIO>
cd WYRD_RASP_BAXTER/server_rasp
Crie e ative o ambiente virtual:

Bash

# Criar (só na primeira vez)
python -m venv .venv

# Ativar (sempre que for trabalhar no projeto)
# Windows
.\.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
Instale as dependências:

Bash

pip install -r requirements.txt
Execute a aplicação:

Bash

# O --host 0.0.0.0 é crucial para que as ESPs possam acessar o servidor
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
Acesse a aplicação: Abra seu navegador e acesse http://127.0.0.1:8000.

3. Cliente (ESP32)
Abra o arquivo .ino na Arduino IDE.

Instale as bibliotecas pelo "Gerenciador de Bibliotecas":

PubSubClient (de Nick O'Leary)

ArduinoJson

Configure as variáveis no topo do ficheiro para corresponder à sua rede: SSID, PASSWORD, SERVER_IP, e MQTT_BROKER_HOST.

Grave o firmware na sua placa ESP32.

Abra o Monitor Serial a uma velocidade de 115200 baud para ver os logs e depurar.

📝 Documentação da API
A documentação interativa da API (gerada automaticamente pelo FastAPI) está disponível nos seguintes endpoints quando o servidor está rodando:

Swagger UI: /docs

ReDoc: /redoc