import paho.mqtt.client as mqtt
import json
import asyncio
from datetime import datetime, timezone
from .models import SessionLocal, Asset, Embarcado
from .config import settings
import logging

logger = logging.getLogger(__name__)

# --- NOVO: Fila assíncrona para comunicação com o agregador ---
scan_data_queue = asyncio.Queue()

_update_task = None

client = mqtt.Client()

def publish_command_to_esp(esp_id: str, command: dict):
    """(Mantida) Publica um comando específico para o canal individual de uma ESP."""
    if not client.is_connected():
        logger.warning(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    command_topic = f"wyrd/rtls/esp/{esp_id}/command"
    payload = json.dumps(command)
    logger.info(f"[MQTT] Enviando comando {payload} para a ESP '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=2)

def on_message(client, userdata, msg):
    """
    Callback para processar mensagens de scan_data, atualizar o last_seen da ESP
    e colocar os dados na fila para o agregador.
    """
    topic_parts = msg.topic.split('/')
    
    if len(topic_parts) == 5 and topic_parts[3] == "scan_data":
        esp_id = topic_parts[2]
        try:
            payload = json.loads(msg.payload)
            scan_data_queue.put_nowait({"esp_id": esp_id, "payload": payload})

            db = SessionLocal()
            try:
                embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
                if embarcado:
                    embarcado.last_seen = datetime.now(timezone.utc)
                    if embarcado.status_rede == 'offline':
                        embarcado.status_rede = 'online'
                        logger.info("ESP %s ficou 'online' ao receber dados de scan.", esp_id)
                    db.commit()
            finally:
                db.close()
        except json.JSONDecodeError:
            logger.warning("[MQTT] JSON inválido recebido no tópico: %s", msg.topic)
        except Exception as e:
            logger.error("[MQTT] Erro ao processar mensagem de %s: %s", esp_id, e)

def on_connect(client, userdata, flags, rc):
    """Callback executado quando a conexão com o broker é (re)estabelecida."""
    if rc == 0:
        logger.info("[MQTT] Conectado com sucesso ao Broker MQTT!")
        
        # --- ALTERADO: Subscrição principal agora é para os dados de scan ---
        client.subscribe("wyrd/rtls/esp/+/scan_data", qos=0)
        logger.info("[MQTT] Subscrito ao tópico de dados 'wyrd/rtls/esp/+/scan_data'")
        
    else:
        logger.error(f"[MQTT] Falha ao conectar, código de retorno: {rc}")

def start_mqtt_client():
    """Inicializa e conecta o cliente MQTT."""
    logger.info("[MQTT] Iniciando cliente...")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.username_pw_set("wyrd_user", "Wyrd2025")
        broker_port = int(settings.get("mqtt_broker_port"))
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")