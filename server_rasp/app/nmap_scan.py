# nmap_scan.py

import subprocess
import re
import platform
from .config import settings

def get_connected_macs():
    """
    Retorna todos os MACs ativos na rede usando um scan ativo do Nmap.
    Este método é confiável tanto no Windows quanto no Linux.

    Pré-requisito: Nmap deve estar instalado e no PATH do sistema.
    """
    network_range = settings.get('network_range_scan')
    
    if not network_range:
        print("[nmap_scan] ERRO CRÍTICO: 'network_range_scan' não definido no seu config.ini.")
        return []

    print(f"[nmap_scan] Usando Nmap para scan ativo em {network_range}...")
    
    # ================== CORREÇÃO AQUI ==================
    # Mudamos o comando para '-PR', que força um ARP scan.
    # Este método é mais robusto para descobrir hosts em uma rede local (LAN).
    cmd = f"nmap -PR {network_range}"
    # ================== FIM DA CORREÇÃO ================
    
    try:
        # Aumentamos o timeout para 3 minutos para dar mais margem.
        result = subprocess.run(
            cmd, 
            shell=True,
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True,
            timeout=180 
        )

        if result.stderr:
            print(f"[nmap_scan] Aviso do Nmap: {result.stderr.strip()}")

        output = result.stdout
        
        mac_addresses = re.findall(r"(?:MAC Address|Endereço MAC): ([\w:]+)", output)
        
        mac_addresses_lower = [mac.lower() for mac in mac_addresses]
        
        print(f"[nmap_scan] MACs encontrados (Nmap): {mac_addresses_lower}")
        return mac_addresses_lower

    except FileNotFoundError:
        print("\n" + "="*60)
        print("!!! ERRO CRÍTICO: Comando 'nmap' não encontrado. !!!")
        print("="*60 + "\n")
        return []
    except subprocess.TimeoutExpired:
        print("[nmap_scan] ERRO: O scan do Nmap demorou mais de 180 segundos e foi interrompido.")
        return []