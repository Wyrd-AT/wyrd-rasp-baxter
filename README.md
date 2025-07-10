# 🏥 Baxter — Sistema de Localização de Camas em Tempo Real

Um sistema de IoT para monitoramento em tempo real da localização de camas hospitalares, construído com FastAPI, MQTT e ESP32.

---

## ✨ Funcionalidades Principais

- **Localização em Tempo Real**  
  Determina em qual quarto cada cama está, com base na intensidade do sinal de beacons Bluetooth.

- **Sincronização via MQTT**  
  Utiliza um broker MQTT para enviar, em tempo real, a lista de camas disponíveis para todos os dispositivos, evitando conflitos e leituras duplicadas.

- **Interface Web de Gestão**  
  Painel administrativo para cadastrar, editar e visualizar o estado de **Camas** e **Dispositivos ESP**.

- **Histórico de Eventos**  
  Log completo de todos os eventos de `GET` e `OUT` recebidos, com informações de RSSI, Wi-Fi e data.

- **Validação de Presença na Rede**  
  Confirma se o módulo Wi-Fi de uma cama está ativo na rede (via `nmap`/`arp`) antes de associá-la a um quarto, aumentando a confiabilidade.

- **Lógica de Agregação Inteligente**  
  Processa eventos de múltiplas fontes, elegendo o ESP com o sinal mais forte como a localização correta da cama.

---

## 🛠️ Tecnologias Utilizadas

- **Backend**: Python 3, FastAPI  
- **Servidor ASGI**: Uvicorn  
- **Banco de Dados**: SQLite  
- **Mensageria**: MQTT (Mosquitto)  
- **Hardware**: ESP32, Beacons BLE  
- **Frontend (Admin)**: HTML5, CSS3, Jinja2  
- **Bibliotecas Python**: SQLAlchemy, Paho-MQTT, python-nmap

---

## 📂 Estrutura do Projeto

WYRD_RASP_BAXTER/
├── .venv/ # Ambiente virtual Python
├── server_rasp/ # Código-fonte principal do servidor
│ ├── app/ # Módulo da aplicação FastAPI
│ │ ├── web/
│ │ │ ├── static/ # CSS, JS, imagens
│ │ │ └── templates/ # Templates Jinja2
│ │ ├── init.py
│ │ └── ... # Demais módulos
│ ├── aggregator.py # Processa eventos, resolve conflitos e atualiza estado
│ ├── auth.py # Autenticação das rotas de admin
│ ├── config.py # Configurações (IPs, portas, tópicos MQTT)
│ ├── dispatcher.py # Envia estado final a sistema externo (Connecta)
│ ├── main.py # Ponto de entrada FastAPI e rotas
│ ├── models.py # Modelos SQLAlchemy (Bed, Embarcado)
│ ├── nmap_scan.py # Lógica de escaneamento de rede
│ ├── presence.py # Verificação de presença de MAC na rede
│ └── mqtt_client.py # Cliente MQTT para publicar atualizações
├── beds.db # Banco de dados SQLite
├── README.md # Este arquivo
└── requirements.txt # Dependências Python

---

## 🚀 Instalação e Execução

### 1. Broker MQTT (Mosquitto)

1. Instale o Mosquitto no seu sistema operacional.  
2. Edite o arquivo `mosquitto.conf` para permitir acesso pela rede:

   ```conf
   listener 1883 0.0.0.0
   allow_anonymous true

Inicie o serviço:

Windows

powershell
Copiar
Editar
net start mosquitto
Linux/macOS

bash
Copiar
Editar
sudo systemctl start mosquitto
2. Backend (Servidor FastAPI)
Clone o repositório e entre na pasta:

bash
Copiar
Editar
git clone <URL_DO_SEU_REPOSITORIO>
cd WYRD_RASP_BAXTER/server_rasp
Crie e ative o ambiente virtual:

bash
Copiar
Editar
# Primeiro uso
python -m venv .venv

# Sempre que for trabalhar
# Windows
.\.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
Instale as dependências:

bash
Copiar
Editar
pip install -r requirements.txt
Execute a aplicação:

bash
Copiar
Editar
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
Acesse no navegador:

cpp
Copiar
Editar
http://127.0.0.1:8000
3. Cliente (ESP32)
Abra o arquivo .ino na Arduino IDE.

Instale as bibliotecas via Gerenciador de Bibliotecas:

PubSubClient (Nick O’Leary)

ArduinoJson

No topo do arquivo, configure:

cpp
Copiar
Editar
const char* SSID             = "SEU_SSID";
const char* PASSWORD         = "SUA_SENHA";
const char* SERVER_IP        = "IP_DO_SERVIDOR";
const char* MQTT_BROKER_HOST = "IP_DO_BROKER";
Grave o firmware na placa ESP32.

Abra o Monitor Serial a 115200 baud para visualizar logs.

📝 Documentação da API
Com o servidor em execução, acesse:

Swagger UI: http://<SERVER_IP>:8000/docs

ReDoc: http://<SERVER_IP>:8000/redoc