"""Point d'entrée rapide : lance l'interface web de Veilleur et ouvre le navigateur.

Dans VS Code, ouvrir ce fichier et cliquer sur « Run Python File » (avec l'interpréteur de
l'environnement où `requirements.txt` est installé), ou en terminal : `python app.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "web_ui"))

from app import main  # noqa: E402  (web_ui/app.py)

if __name__ == "__main__":
    main()
