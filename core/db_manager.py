"""
Re-export del Database Manager per compatibilità architetturale con FlowLocal core.
Tutte le operazioni fanno riferimento a database.db_manager.
"""

from database.db_manager import DatabaseManager, db, DB_PATH

__all__ = ["DatabaseManager", "db", "DB_PATH"]

