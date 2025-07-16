# config.py

# Endereço e porta do servidor TCP
IP = "172.28.74.165"
FINAL_IP = "172.28.74.51"
PORT = 8000
FINAL_PORT = 9500

# Rede usada nos scans
NETWORK_RANGE = "172.28.74.0/24"

NETWORK_PREFIX = "172.28.74."

# Historiador
HISTORY_RETENTION_DAYS = 7       # mantém apenas 7 dias de eventos
EVENT_PAGE_SIZE         = 20     # linhas por página em /events
CLEANUP_INTERVAL_SEC    = 3600   # a cada hora roda a limpeza

# MQTT
MQTT_BROKER_HOST = "172.28.74.165" # ou o IP do seu PC
MQTT_BROKER_PORT = 1883
MQTT_BED_LIST_TOPIC = "wyrd/baxter/beds/available"

WARNING_DELAY_MINUTES = 5