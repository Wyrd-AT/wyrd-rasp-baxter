# app/bed_mqtt_client.py (VERSÃO BAXTER 2.0)

import paho.mqtt.client as mqtt
import json
import asyncio
from .config import settings
import logging

logger = logging.getLogger(__name__)

# Fila dedicada para dados de status da cama (conectado/desconectado)
# Esta fila será consumida pelo 'bed_state_processor_loop' no main.py
bed_state_queue = asyncio.Queue()

# Cria um objeto cliente SEPARADO do cliente principal (o principal ouve os ESPs)
bed_client = mqtt.Client()

def on_bed_message(client, userdata, msg):
    """Callback focado APENAS nas mensagens das Camas Hillrom."""
    topic = msg.topic
    
    try:
        payload_str = msg.payload.decode('utf-8')
        payload_json = json.loads(payload_str)
        
        # Coloca a mensagem na fila para o main.py processar de forma assíncrona
        # O loop de eventos do FastAPI cuidará de retirar e processar
        bed_state_queue.put_nowait(payload_json) 
        
        logger.debug(f"[MQTT-CAMA] Mensagem recebida da cama '{payload_json.get('id')}'. Enfileirada.")
        
    except json.JSONDecodeError:
        logger.warning(f"[MQTT-CAMA] JSON inválido recebido no tópico de cama: {topic}")
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Erro ao processar mensagem de cama: {e}")

def on_bed_connect(client, userdata, flags, rc):
    """Callback de conexão."""
    if rc == 0:
        logger.info("[MQTT-CAMA] Conectado ao Broker de Camas (Hillrom)!")
        
        # Tópico padrão da Hillrom para status de conexão/locação
        topic = "2.0/HIAE/hillrom/bed/+/json/state"
        
        client.subscribe(topic, qos=0)
        logger.info(f"[MQTT-CAMA] Subscrito ao tópico: '{topic}'")
    else:
        logger.error(f"[MQTT-CAMA] Falha ao conectar ao broker de camas, código: {rc}")

def start_bed_client():
    """Inicializa o cliente MQTT dedicado para as Camas."""
    
    # Lê as configurações específicas para este broker no config.ini
    broker_host = settings.get("bed_mqtt_host", "127.0.0.1")
    broker_port = int(settings.get("bed_mqtt_port", 1883))
    username = settings.get("bed_mqtt_user", None)
    password = settings.get("bed_mqtt_pass", None)

    logger.info(f"[MQTT-CAMA] Iniciando conexão com {broker_host}:{broker_port}...")

    if username and password:
        bed_client.username_pw_set(username, password)

    bed_client.on_connect = on_bed_connect
    bed_client.on_message = on_bed_message

    try:
        # Conecta e inicia o loop em uma thread separada (loop_start)
        # para não bloquear o FastAPI
        bed_client.connect(broker_host, broker_port, 60)
        bed_client.loop_start()
    except Exception as e:
        logger.error(f"[MQTT-CAMA] Não foi possível conectar ao broker de camas: {e}")