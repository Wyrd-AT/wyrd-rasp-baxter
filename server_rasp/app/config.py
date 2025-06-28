# config.py

# Endereço e porta do servidor TCP
IP = "192.168.1.213"
FINAL_IP = "192.168.1.183"
PORT = 9500

# Rede usada nos scans
NETWORK_RANGE = "192.168.1.0/24"

NETWORK_PREFIX = "192.168.1."

# Historiador
HISTORY_RETENTION_DAYS = 7       # mantém apenas 7 dias de eventos
EVENT_PAGE_SIZE         = 50     # linhas por página em /events
CLEANUP_INTERVAL_SEC    = 3600   # a cada hora roda a limpeza