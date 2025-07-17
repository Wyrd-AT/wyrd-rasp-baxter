# run.py
import uvicorn
import sys
import logging # Importa a biblioteca de logging

# Importa o dicionário 'settings' do novo config.py
from app.config import settings

# --- INÍCIO DA MODIFICAÇÃO ---

# 1. Criação do Filtro Personalizado
# Esta classe irá inspecionar cada log e bloquear aqueles com status 304.
class No304Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # A mensagem de acesso do Uvicorn vem em record.args
        # Ex: ('127.0.0.1:50154', 'GET /static/css/style.css HTTP/1.1', 304)
        # Verificamos se há argumentos e se o terceiro argumento (status code) é 304.
        return not (len(record.args) >= 3 and record.args[2] == 304)

# --- FIM DA MODIFICAÇÃO ---


# Define uma configuração de log básica que não depende de um console.
# Isso evita o erro 'isatty' ao rodar como um executável com --windowed.
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "use_colors": False,
        },
    },
    # --- INÍCIO DA MODIFICAÇÃO ---

    # 2. Registra o nosso filtro
    "filters": {
        "no_304": {
            "()": No304Filter,
        }
    },

    # --- FIM DA MODIFICAÇÃO ---
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["no_304"], # 3. Aplica o filtro ao handler de acesso
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

if __name__ == '__main__':
    # Importa o objeto principal do seu aplicativo
    from app.main import app

    try:
        # Pega os valores do dicionário carregado do config.ini
        # Usa 'get' com um valor padrão para segurança
        host_ip = settings.get('ip', '0.0.0.0')
        port_int = int(settings.get('port', 8000))
    except (ValueError, TypeError):
        # Fallback caso haja erro na conversão do config.ini
        host_ip = '0.0.0.0'
        port_int = 8000

    # Inicia o servidor Uvicorn com as configurações corretas
    uvicorn.run(
        app,
        host=host_ip,
        port=port_int,
        log_config=LOGGING_CONFIG
    )