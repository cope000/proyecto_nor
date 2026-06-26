"""Carga de configuración desde .env y constantes de conexión."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env desde la raíz del proyecto
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_env_path)

# Credenciales
USER: str = os.getenv("PYROFEX_USER", "")
PASSWORD: str = os.getenv("PYROFEX_PASSWORD", "")
ACCOUNT: str = os.getenv("PYROFEX_ACCOUNT", "")
ENV: str = os.getenv("PYROFEX_ENV", "REMARKET")

# Validación mínima
if not all([USER, PASSWORD, ACCOUNT]):
    raise RuntimeError(
        "Faltan credenciales en .env. Revisá PYROFEX_USER, PYROFEX_PASSWORD y PYROFEX_ACCOUNT."
    )
