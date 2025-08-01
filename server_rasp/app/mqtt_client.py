# mqtt_client.py (versão corrigida com a função que faltava)
import paho.mqtt.client as mqtt
import json
import asyncio
from .connection_manager import manager
from datetime import datetime, timezone, timedelta
from .models import SessionLocal, Asset, Embarcado
from .config import settings
import logging
logger = logging.getLogger(__name__)

# --- ESTRUTURA ADICIONADA ---
_publish_queue = []
esp_heartbeats = {}

# --- Cliente MQTT (sem alteração) ---
client = mqtt.Client()

_update_task = None

async def _publish_debounced():
    global _update_task
    try:
        await asyncio.sleep(3)
        logger.info("[DEBOUNCER] Janela de 3s fechada. Publicando a lista de ativos consolidados.")
        
        # Chama a função que já existe neste ficheiro
        publish_available_assets()
        
        _update_task = None
    except asyncio.CancelledError:
        logger.error("[DEBOUNCER] Publicação de ativos adiada por uma nova mudança.")
        raise

def schedule_asset_list_update():
    """
    Agenda a publicação da lista de ativos. Se já houver uma agendada,
    cancela a antiga e cria uma nova (debounce).
    """
    global _update_task
    if _update_task:
        _update_task.cancel()
    
    _update_task = asyncio.create_task(_publish_debounced())

def get_available_assets_macs():
    """Busca no banco de dados os MACs de BEACONS dos ativos disponíveis."""
    db = SessionLocal()
    try:
        available_assets = db.query(Asset.mac_beacon).filter(Asset.quarto_id.is_(None)).all()
        mac_list = [mac for mac, in available_assets if mac]
        return mac_list
    finally:
        db.close()

def publish_available_assets():
    """Publica a lista de ativos disponíveis. Se offline, enfileira a publicação."""
    mac_list = get_available_assets_macs()
    payload = json.dumps(mac_list)
    topic = settings.get('mqtt_asset_list_topic')

    if not client.is_connected():
        logger.info(f"[MQTT] Cliente não conectado. Enfileirando publicação para o tópico '{topic}'.")
        _publish_queue.append({'topic': topic, 'payload': payload, 'qos': 1, 'retain': True})
        return

    logger.info(f"[MQTT] Publicando lista de ATIVOS disponíveis no tópico '{topic}': {payload}")
    client.publish(topic, payload, qos=1, retain=True)

def publish_verdict(esp_id: str, status: str, beacon_mac: str, transacao_id: int):
    """Publica o resultado de uma disputa para uma ESP específica, incluindo o ID da transação."""
    if not client.is_connected():
        logger.info(f"[MQTT] Cliente não conectado. Abortando envio de veredito para {esp_id}.")
        return

    verdict_topic = f"wyrd/rtls/esp/{esp_id}/verdict"
    payload = json.dumps({
        "status": status,
        "ativo": beacon_mac,
        "transacao_id": transacao_id
    })
    
    logger.info(f"[MQTT] Enviando veredito '{status}' (ID: {transacao_id}) para a ESP '{esp_id}' no tópico '{verdict_topic}'")
    client.publish(verdict_topic, payload, qos=2)

# --- FUNÇÃO QUE ESTAVA FALTANDO ---
def publish_command_to_esp(esp_id: str, command: dict):
    """Publica um comando específico para o canal individual de uma ESP."""
    if not client.is_connected():
        logger.info(f"[MQTT] Cliente não conectado. Abortando envio de comando para {esp_id}.")
        return

    # Este tópico deve ser compatível com o que o ESP espera
    command_topic = f"wyrd/rtls/esp/{esp_id}/command"
    payload = json.dumps(command)

    logger.info(f"[MQTT] Enviando comando {payload} para a ESP '{esp_id}' no tópico '{command_topic}'")
    client.publish(command_topic, payload, qos=2)
# --- FIM DA FUNÇÃO QUE ESTAVA FALTANDO ---

def on_message(client, userdata, msg):
    """Callback para processar mensagens de heartbeat."""
    topic_parts = msg.topic.split('/')
    
    if len(topic_parts) == 5 and topic_parts[3] == "heartbeat":
        esp_id = topic_parts[4]
        db = SessionLocal()
        try:
            embarcado = db.query(Embarcado).filter(Embarcado.id_esp == esp_id).first()
            if embarcado:
                embarcado.last_seen = datetime.now(timezone.utc)
                if embarcado.status_rede == 'offline':
                    embarcado.status_rede = 'online'
                    logger.info("ESP %s voltou a ficar online.", esp_id)
                db.commit()
        finally:
            db.close()
        return

def on_connect(client, userdata, flags, rc):
    """Callback executado quando a conexão com o broker é (re)estabelecida."""
    if rc == 0:
        logger.info("[MQTT] Conectado com sucesso ao Broker MQTT!")

        client.subscribe("wyrd/rtls/esp/heartbeat/+")
        logger.info("[MQTT] Subscrito ao tópico de heartbeats 'wyrd/rtls/esp/heartbeat/+'")

        publish_available_assets()

        if _publish_queue:
            logger.info(f"[MQTT] Enviando {_publish_queue.__len__()} mensagens da fila de espera...")
            for msg in list(_publish_queue):
                client.publish(
                    topic=msg['topic'],
                    payload=msg['payload'],
                    qos=msg.get('qos', 1),
                    retain=msg.get('retain', False)
                )
                _publish_queue.remove(msg)
            logger.info("[MQTT] Fila de mensagens processada.")
    else:
        logger.info(f"[MQTT] Falha ao conectar, código de retorno: {rc}")

def connect_mqtt():
    """Inicia a conexão com o broker MQTT."""
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        broker_port = int(settings.get("mqtt_broker_port"))
        client.connect(settings.get("mqtt_broker_host"), broker_port, 60)
        client.loop_start()
    except Exception as e:
        logger.error(f"[MQTT] Não foi possível conectar ao broker: {e}")

def start_mqtt_client():
    """
    Inicializa e conecta o cliente MQTT.
    """
    logger.info("[MQTT] Iniciando cliente...")
    connect_mqtt()