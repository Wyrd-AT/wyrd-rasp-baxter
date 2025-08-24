# ==============================================================================
# ARQUIVO: presence.py (Versão Assíncrona)
# ==============================================================================
import time
# Importa as novas funções assíncronas do módulo de scan
from .nmap_scan import get_mac_to_ip_map_async, is_host_online_async

import logging
logger = logging.getLogger(__name__)

# Estrutura de Cache para o Mapa
_mac_ip_map_cache = {}
_cache_last_updated = 0
CACHE_TTL_SECONDS = 15 # Define a validade do cache (em segundos)

async def check_presence(mac: str) -> bool:
    """
    Versão ASSÍNCRONA que verifica a presença de um MAC com uma lógica de
    cache-miss para máxima fiabilidade e performance, sem bloquear o servidor.
    """
    global _mac_ip_map_cache, _cache_last_updated
    
    #logger.info(f"[presence_async] Verificando presença do MAC: {mac}")
    target_mac = mac.lower()
    now = time.time()
    
    # 1. Verifica se o cache expirou
    if not _mac_ip_map_cache or (now - _cache_last_updated > CACHE_TTL_SECONDS):
        #logger.info("[presence_async] Cache do mapa MAC->IP expirado. Atualizando...")
        _mac_ip_map_cache = await get_mac_to_ip_map_async()
        _cache_last_updated = now
        
    # 2. Tenta encontrar o IP no cache atual
    target_ip = _mac_ip_map_cache.get(target_mac)
    
    # 3. Lógica de Cache-Miss: Se não encontrou, o cache pode estar desatualizado.
    #    Força uma nova leitura da rede para garantir.
    if not target_ip:
        #logger.info(f"[presence_async] MAC {target_mac} não encontrado no cache. Forçando atualização da rede...")
        _mac_ip_map_cache = await get_mac_to_ip_map_async()
        _cache_last_updated = now
        
        # Tenta encontrar o IP novamente no mapa recém-criado
        target_ip = _mac_ip_map_cache.get(target_mac)

    # 4. Se encontrou um IP (seja no cache ou após a atualização), faz a verificação ativa
    if target_ip:
        return await is_host_online_async(target_ip)
    
    # 5. Se mesmo após forçar a atualização o MAC não foi encontrado, ele está offline
    logger.info(f"[presence_async] MAC {target_mac} não foi encontrado no mapa da rede. Considerado offline.")
    return False