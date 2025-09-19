# ==============================================================================
# ARQUIVO: presence.py (Versão Assíncrona e Robusta)
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
    Versão robusta que verifica a presença e re-confirma o MAC para evitar
    falsos positivos causados por cache ARP obsoleto e reatribuição de IP por DHCP.
    """
    global _mac_ip_map_cache, _cache_last_updated
    
    target_mac = mac.lower()
    now = time.time()
    
    # PASSO 1: Tenta encontrar o IP do ativo
    if not _mac_ip_map_cache or (now - _cache_last_updated > CACHE_TTL_SECONDS):
        _mac_ip_map_cache = await get_mac_to_ip_map_async()
        _cache_last_updated = now
        
    target_ip = _mac_ip_map_cache.get(target_mac)
    
    # Lógica de Cache-Miss: Se não encontrou, força uma nova atualização.
    if not target_ip:
        _mac_ip_map_cache = await get_mac_to_ip_map_async()
        _cache_last_updated = now
        target_ip = _mac_ip_map_cache.get(target_mac)

    # Se mesmo após a atualização o MAC não tem um IP, ele está offline.
    if not target_ip:
        logger.debug(f"[presence_async] MAC {target_mac} não encontrado no mapa da rede. Considerado offline.")
        return False

    # PASSO 2: Verifica se o IP está respondendo
    is_online = await is_host_online_async(target_ip)
    if not is_online:
        logger.debug(f"[presence_async] IP {target_ip} não respondeu ao Nmap. Considerado offline.")
        return False
        
    # PASSO 3: Se o IP respondeu, verifica se o MAC ainda pertence a ele.
    # Isso protege contra o caso de outro dispositivo ter assumido o IP.
    logger.debug(f"[presence_async] IP {target_ip} está online. Re-verificando o MAC associado...")
    
    # Força uma nova leitura da tabela ARP, que foi recentemente atualizada pelo Nmap no passo anterior.
    current_mac_map = await get_mac_to_ip_map_async()
    
    # Inverte o mapa para facilitar a busca por IP
    ip_to_mac_map = {ip: mac for mac, ip in current_mac_map.items()}
    
    current_mac_for_ip = ip_to_mac_map.get(target_ip)
    
    if not current_mac_for_ip:
        logger.warning(f"[presence_async] IP {target_ip} está online, mas não foi encontrado na tabela ARP mais recente. Inconsistência de rede.")
        return False
        
    if current_mac_for_ip == target_mac:
        logger.debug(f"[presence_async] SUCESSO: O MAC para o IP {target_ip} foi confirmado como {target_mac}.")
        return True
    else:
        logger.warning(f"[presence_async] FALHA DE VERIFICAÇÃO: O IP {target_ip} está online, mas agora pertence ao MAC {current_mac_for_ip} (esperado: {target_mac}).")
        return False