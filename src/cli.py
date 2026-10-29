#!/usr/bin/env python3
"""Точка входа для remote_deployer. Работает как из исходников, так и из .exe."""
import sys
import os

# При сборке PyInstaller добавляет свой путь; при обычном запуске — добавляем src/
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

from remote_deployer import main

if __name__ == "__main__":
    main()