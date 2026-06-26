"""Script temporal — validar get_display_name en todos los bots."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.bot_manager import BotManager

mgr = BotManager()
for bot_id in mgr.registry.keys():
    try:
        name = mgr.get_display_name(bot_id)
        static = mgr.registry[bot_id].get("name", "?")
        match = "=" if static == name else "→"
        print(f"  {bot_id:15s}  static='{static}'  {match}  dynamic='{name}'")
    except Exception as e:
        print(f"  {bot_id:15s}  ERROR: {e}")
