# ==============================================================================
# ARQUIVO: nmap_scan.py (Versão Final, Corrigida e Compatível com Windows)
# ==============================================================================
import asyncio
import re
import subprocess
from .config import settings

import logging
logger = logging.getLogger(__name__)

# --- Funções "Trabalhadoras" (Síncronas) ---

def _worker_get_mac_to_ip_map() -> dict:
    """Função trabalhadora que executa o 'arp -a' e processa o resultado."""
    try:
        # Usamos uma codificação compatível com o CMD do Windows em português
        result = subprocess.run(
            "arp -a",
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            encoding='cp850'
        )
        if result.returncode != 0:
            logger.info(f"[nmap_scan_worker] ERRO: Comando 'arp -a' falhou. Stderr: {result.stderr}")
            return {}

        output = result.stdout
        pattern = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})")
        matches = pattern.findall(output)
        mac_ip_map = {mac.lower().replace('-', ':'): ip for ip, mac in matches}
        
        #logger.info(f"[nmap_scan_worker] Mapa MAC->IP atualizado. {len(mac_ip_map)} dispositivos encontrados.")
        return mac_ip_map
    except Exception as e:
        logger.info(f"[nmap_scan_worker] ERRO CRÍTICO ao criar mapa MAC->IP: {e}")
        return {}

def _worker_is_host_online(ip_address: str) -> bool:
    """Função trabalhadora que executa o 'nmap' e procura pela resposta correta."""
    if not ip_address:
        return False
        
    try:
        command = ["nmap", "-sn", "-PE", "-PR", "-T4", ip_address]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15
        )
        
        # --- CORREÇÃO PRINCIPAL AQUI ---
        # Procuramos por "Host is up" em vez de "Status: Up"
        if "Host is up" in result.stdout:
            return True
        else:
            return False
        # --- FIM DA CORREÇÃO ---
            
    except FileNotFoundError:
        logger.info("\n\n[NMAP] ERRO CRÍTICO: O comando 'nmap' não foi encontrado. Instale o Nmap no seu sistema.\n\n")
        return False
    except Exception as e:
        logger.info(f"[nmap_scan_worker] Erro ao executar Nmap para o IP {ip_address}: {e}")
        return False

# --- Funções de Interface (Assíncronas) ---

async def get_mac_to_ip_map_async() -> dict:
    """Interface assíncrona que chama a função trabalhadora numa thread separada."""
    #logger.info("[nmap_scan_async] Agendando atualização de mapa MAC->IP...")
    loop = asyncio.get_running_loop()
    mac_ip_map = await loop.run_in_executor(None, _worker_get_mac_to_ip_map)
    return mac_ip_map

async def is_host_online_async(ip_address: str) -> bool:
    """Interface assíncrona que chama a verificação ativa do Nmap numa thread separada."""
    #logger.info(f"[nmap_scan_async] Agendando verificação ativa do IP: {ip_address} com Nmap...")
    loop = asyncio.get_running_loop()
    is_online = await loop.run_in_executor(None, _worker_is_host_online, ip_address)
    
    if is_online:
        logger.info(f"[nmap_scan_async] SUCESSO: Host {ip_address} está online.")
    else:
        logger.info(f"[nmap_scan_async] FALHA: Host {ip_address} parece estar offline.")
        
    return is_online