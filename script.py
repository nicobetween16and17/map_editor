import sys
import os
import math
import json
from collections import defaultdict
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsPixmapItem,
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsItemGroup,
    QGraphicsTextItem,
    QGraphicsDropShadowEffect,
    QMainWindow,
    QToolBar,
    QAction,
    QActionGroup,
    QColorDialog,
    QFileDialog,
    QMessageBox,
    QComboBox,
    QLabel,
    QPushButton,
    QButtonGroup,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QWidget,
    QHBoxLayout,
    QTabWidget,
    QTabBar,
    QInputDialog,
)
from PyQt5.QtGui import QPolygonF, QBrush, QColor, QPen, QPainter, QPixmap, QFont, QKeySequence, QIcon
from PyQt5.QtCore import QPointF, Qt, QTimer, QSize, QObject, pyqtSignal
from enum import Enum
import socket
import secrets
import threading
TILE_WIDTH = 64
TILE_HEIGHT = 32
ROWS = 16
COLS = 18

#
# RESEAU
#

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def generate_password():
    return f"{secrets.randbelow(1_000_000):06d}"


class NetworkBridge(QObject):
    op_received = pyqtSignal(dict)
    snapshot_received = pyqtSignal(dict)
    auth_failed = pyqtSignal()
    client_count_changed = pyqtSignal(int)
    disconnected = pyqtSignal()


class HostServer:
    def __init__(self, scene, password, bridge):
        self.scene = scene
        self.password = password
        self.bridge = bridge
        self.clients = []
        self.server_socket = None
        self._running = False

    def start(self, port=5555):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", port))
        self.server_socket.listen(5)
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn):
        try:
            data = conn.recv(4096).decode("utf-8")
            msg = json.loads(data.strip())
            if msg.get("type") != "auth" or msg.get("password") != self.password:
                conn.sendall((json.dumps({"type": "auth_fail"}) + "\n").encode("utf-8"))
                conn.close()
                return
        except (OSError, json.JSONDecodeError):
            conn.close()
            return

        snapshot_msg = {"type": "auth_ok", "snapshot": self.scene.to_dict()}
        conn.sendall((json.dumps(snapshot_msg) + "\n").encode("utf-8"))
        self.clients.append(conn)
        self.bridge.client_count_changed.emit(len(self.clients))

        buffer = ""
        while self._running:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        m = json.loads(line)
                        if m.get("type") == "op":
                            self.bridge.op_received.emit(m["op"])
                            self.broadcast(m, exclude=conn)
            except (OSError, json.JSONDecodeError):
                break

        if conn in self.clients:
            self.clients.remove(conn)
        self.bridge.client_count_changed.emit(len(self.clients))
        conn.close()

    def broadcast(self, msg, exclude=None):
        data = (json.dumps(msg) + "\n").encode("utf-8")
        for c in list(self.clients):
            if c is not exclude:
                try:
                    c.sendall(data)
                except OSError:
                    pass

    def send_op(self, op):
        self.broadcast({"type": "op", "op": op})

    def stop(self):
        self._running = False
        if self.server_socket:
            self.server_socket.close()
        for c in self.clients:
            c.close()


class ClientConnection:
    def __init__(self, bridge):
        self.bridge = bridge
        self.sock = None
        self._running = False

    def connect(self, ip, port, password):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((ip, port))
        self.sock.sendall((json.dumps({"type": "auth", "password": password}) + "\n").encode("utf-8"))
        self.sock.settimeout(None)
        self._running = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _listen_loop(self):
        buffer = ""
        while self._running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg["type"] == "auth_fail":
                        self.bridge.auth_failed.emit()
                        return
                    elif msg["type"] == "auth_ok":
                        self.bridge.snapshot_received.emit(msg["snapshot"])
                    elif msg["type"] == "op":
                        self.bridge.op_received.emit(msg["op"])
            except (OSError, json.JSONDecodeError):
                break
        self.bridge.disconnected.emit()

    def send_op(self, op):
        if self.sock:
            try:
                self.sock.sendall((json.dumps({"type": "op", "op": op}) + "\n").encode("utf-8"))
            except OSError:
                pass

    def disconnect(self):
        self._running = False
        if self.sock:
            self.sock.close()

class EditorMode(Enum):
    PAINT = 0
    TOKEN = 1
    ARROW = 2
    ERASE = 3

#
# TOKENS/INFO
#

ASSETS_DIR = "assets/tokens"

TOKEN_RADIUS = 20

ELEMENT_COLORS = {
    "eau": QColor(0, 120, 255, 50),
    "feu": QColor(255, 50, 50, 50),
    "terre": QColor(46, 139, 87, 50),
    "air": QColor(150, 0, 255, 50),
    "default": QColor(255, 255, 255, 120),
}

TOKEN_LIBRARY = {
    "feca": {"label": "Féca", "image": "feca.png", "color": QColor(120, 170, 90), "offset_y": 0, "category": "player"},
    "osamodas": {"label": "Osamodas", "image": "osamodas.png", "color": QColor(150, 110, 60), "offset_y": 0,
                 "category": "player"},
    "enutrof": {"label": "Enutrof", "image": "enutrof.png", "color": QColor(160, 140, 40), "offset_y": 0,
                "category": "player"},
    "sram": {"label": "Sram", "image": "sram.png", "color": QColor(70, 70, 80), "offset_y": 0, "category": "player"},
    "xelor": {"label": "Xelor", "image": "xelor.png", "color": QColor(90, 90, 160), "offset_y": 0, "category": "player",
              "children": ["xelor_cadran", "rouage", "sinistro", "regu"]},
    "ecaflip": {"label": "Ecaflip", "image": "ecaflip.png", "color": QColor(200, 150, 60), "offset_y": 0,
                "category": "player"},
    "eniripsa": {"label": "Eniripsa", "image": "eniripsa.png", "color": QColor(220, 120, 150), "offset_y": 0,
                 "category": "player"},
    "iop": {"label": "Iop", "image": "iop.png", "color": QColor(200, 60, 60), "offset_y": 0, "category": "player"},
    "cra": {"label": "Cra", "image": "cra.png", "color": QColor(60, 140, 90), "offset_y": 0, "category": "player"},
    "sadida": {"label": "Sadida", "image": "sadida.png", "color": QColor(90, 150, 60), "offset_y": 0,
               "category": "player",
               "children": ["surpuissante", "bloqueuse", "sacrif", "goulue", "arbre", "gonflable", "graine"]},
    "sacrieur": {"label": "Sacrieur", "image": "sacrieur.png", "color": QColor(150, 40, 40), "offset_y": 0,
                 "category": "player"},
    "pandawa": {"label": "Pandawa", "image": "pandawa.png", "color": QColor(90, 60, 40), "offset_y": 0,
                "category": "player"},
    "roublard": {"label": "Roublard", "image": "roublard.png", "color": QColor(60, 60, 60), "offset_y": 0,
                 "category": "player"},
    "zobal": {"label": "Zobal", "image": "zobal.png", "color": QColor(140, 90, 140), "offset_y": 0,
              "category": "player"},
    "eliotrope": {"label": "Eliotrope", "image": "eliotrope.png", "color": QColor(90, 60, 160), "offset_y": 0,
                  "category": "player"},
    "huppermage": {"label": "Huppermage", "image": "huppermage.png", "color": QColor(160, 60, 160), "offset_y": 0,
                   "category": "player"},
    "ouginak": {"label": "Ouginak", "image": "ouginak.png", "color": QColor(120, 90, 60), "offset_y": 0,
                "category": "player"},
    "steamer": {"label": "Steamer", "image": "steamer.png", "color": QColor(150, 150, 60), "offset_y": 0,
                "category": "player", "children": ["steamer_microbot", "steamer_turret"]},
    # tokens speciaux / mecanismes-invocations
    "xelor_cadran": {"label": "Cadran (Xelor)", "image": "xelor_cadran.png", "color": QColor(90, 90, 200),
                     "hover_range": 3, "offset_y": 8, "category": "mechanism", "unique": True},
    "eni_lapin": {"label": "Lapin (Eniripsa)", "image": "eni_lapin.png", "color": QColor(230, 150, 180), "offset_y": 0,
                  "category": "mechanism", "unique": True},
    "surpuissante": {"label": "Surpuissante (Sadida)", "image": "sadida_surpuissante.png", "color": QColor(60, 180, 60),
                     "offset_y": -8, "category": "mechanism"},
    "bloqueuse": {"label": "bloqueuse (Sadida)", "image": "bloqueuse.png", "color": QColor(60, 180, 60), "offset_y": -8,
                  "category": "mechanism"},
    "goulue": {"label": "goulue (Sadida)", "image": "goulue.png", "color": QColor(60, 180, 60), "offset_y": -7,
               "category": "mechanism"},
    "gonflable": {"label": "gonflable (Sadida)", "image": "gonflable.png", "color": QColor(60, 180, 60), "offset_y": -2,
                  "category": "mechanism"},
    "arbre": {"label": "arbre (Sadida)", "image": "arbre.png", "color": QColor(60, 180, 60), "offset_y": -1,
              "category": "mechanism"},
    "sacrif": {"label": "sacrif (Sadida)", "image": "sacrif.png", "color": QColor(60, 180, 60), "offset_y": -1,
               "category": "mechanism"},
    "graine": {"label": "graine (Sadida)", "image": "graine.png", "color": QColor(60, 180, 60), "offset_y": -16,
               "category": "mechanism"},
    "steamer_microbot": {"label": "Microbot (Steamer)", "image": "steamer_microbot.png", "color": QColor(200, 200, 60),
                         "rail": True, "offset_y": -16, "category": "mechanism"},
    "steamer_turret": {"label": "Tourelle (Steamer)", "image": "tourelle.png", "color": QColor(180, 130, 40),
                       "offset_y": 0, "category": "mechanism"},
    "rouage": {"label": "rouage (Xelor)", "image": "rouage.png", "color": QColor(180, 130, 40), "offset_y": -8,
               "category": "mechanism"},
    "regu": {"label": "regulateur (Xelor)", "image": "regu.png", "color": QColor(180, 130, 40), "offset_y": -4,
             "category": "mechanism"},
    "sinistro": {"label": "sinistro (Xelor)", "image": "sinistro.png", "color": QColor(180, 130, 40), "offset_y": -2,
                 "category": "mechanism"},
    # token auto : se pose tout seul sur les microbots des qu'ils sont alignes par 2 ou plus
    "steamer_rail": {"label": "Rail (Steamer)", "image": "steamer_rail.png", "color": QColor(90, 120, 140),
                     "auto_only": True, "offset_y": -16, "category": "mechanism"},
    # totem Nox : direction (N/E/S/O, tournee au clic droit) + niveau (1-6, molette au
    # survol) ; sa zone d'effet au survol depend en plus de l'element choisi dans la
    # toolbar (eau/air/feu/terre). Voir NoxToken.
    "nox": {"label": "Totem Nox", "image": "nox.png", "color": QColor(80, 80, 95), "offset_y": 0, "category": "nox",
            "unique": True},
}

RAIL_MAX_LENGTH = 8  # deux microbots ne forment un rail que si la case de depart a la case d'arrivee incluses tient sur 8 cases max
MAX_PLAYERS = 6  # nombre maximum de tokens de categorie "player" simultanement sur la carte
ARROW_COLORS = [
    QColor("dodgerblue"),
    QColor("deeppink"),
    QColor("orange"),
    QColor("limegreen"),
    QColor("gray"),
    QColor("red"),
]
ARROW_DEFAULT_COLOR = QColor("white")

# ---------- geometrie des zones du totem Nox ----------
DIRECTION_VECTORS = {"N": (-1, 0), "S": (1, 0), "E": (0, 1), "O": (0, -1)}
OPPOSITE_DIRECTION = {"N": "S", "S": "N", "E": "O", "O": "E"}


# calculs de zones et affichage de tokens
def cone_cells(apex, direction, depth):
    """Cone triangulaire classique : ouvert dans `direction`, apex a `apex`, la rangee
    a distance d (1..depth) a une largeur de 2d-1 cases (large loin de l'apex).
    Ajoute aussi les 4 diagonales partant de l'apex, chacune limitee a `depth` cases."""
    dr, dc = DIRECTION_VECTORS[direction]
    pr, pc = -dc, dr
    cells = set()
    for d in range(1, depth + 1):
        base_r = apex[0] + dr * d
        base_c = apex[1] + dc * d
        for w in range(-(d - 1), d):
            cells.add((base_r + pr * w, base_c + pc * w))

    diagonal_vectors = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    for ddr, ddc in diagonal_vectors:
        for d in range(1, depth + 1):
            cells.add((apex[0] + ddr * d, apex[1] + ddc * d))

    return cells
def terre_cone_cells(apex, direction, depth):
    """Cone de la zone Terre : contrairement a cone_cells, la base (la plus large,
    2*depth-1 cases) est CENTREE SUR NOX (distance 0, sa propre rangee), et la forme
    se resserre en s'eloignant vers `direction` jusqu'a 1 case a distance depth-1."""
    dr, dc = DIRECTION_VECTORS[direction]
    pr, pc = -dc, dr
    cells = set()
    for d in range(0, depth):
        width = 2 * (depth - d) - 1
        base_r = apex[0] + dr * d
        base_c = apex[1] + dc * d
        half = (width - 1) // 2
        for w in range(-half, half + 1):
            cells.add((base_r + pr * w, base_c + pc * w))
    return cells
def populate_token_visual(group, key, cx, cy, radius):
    data = TOKEN_LIBRARY[key]
    path = os.path.join(ASSETS_DIR, data["image"])
    pixmap = QPixmap(path)

    if not pixmap.isNull():
        target_width = radius * 2
        max_height = radius * 5

        # On ne redimensionne plus le pixmap lui-meme : ca figerait sa resolution une
        # fois pour toutes, donc du flou/pixelise des qu'on zoome dans la vue. A la
        # place, on garde l'image a sa resolution native et on applique un simple
        # facteur d'echelle sur l'item graphique — Qt la re-lisse proprement a chaque
        # niveau de zoom, en repartant toujours de la pleine resolution d'origine.
        scale_factor = target_width / pixmap.width()
        if pixmap.height() * scale_factor > max_height:
            scale_factor = max_height / pixmap.height()

        visual = QGraphicsPixmapItem(pixmap)
        visual.setTransformationMode(Qt.SmoothTransformation)
        visual.setScale(scale_factor)

        w = pixmap.width() * scale_factor
        h = pixmap.height() * scale_factor
        visual.setPos(cx - w / 2, cy - h / 2)

        group.addToGroup(visual)
    else:
        print("pas d'assets pour ce token")
#COMBO LIST
def build_token_icon(key, size=24):
    data = TOKEN_LIBRARY[key]
    path = os.path.join(ASSETS_DIR, data["image"])
    pixmap = QPixmap(path)
    if pixmap.isNull():
        pixmap = QPixmap(size, size)
        pixmap.fill(data["color"])
    else:
        pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QIcon(pixmap)
#Gere les tokens joueur/mecanismes
class TokenItem(QGraphicsItemGroup):
    """Token pose sur la carte. Gere son propre visuel (image ou fallback), le
    highlight de zone au survol pour les types qui le prevoient (hover_range), et le
    glisser-depose : rester appuye ~0.5s "attrape" le token, le relacher sur une autre
    case le deplace, le relacher hors grille le repose a sa case de depart."""
    HOLD_MS = 100
    def __init__(self, key, row, col, radius=TOKEN_RADIUS, offset=(0, 0), z=200):
        super().__init__()
        self.key = key
        self.row = row
        self.col = col
        self._origin_key = (row, col)
        self._z_normal = z
        self._drag_active = False
        self._drag_offset = None
        self._arrow_dragging = False
        data = TOKEN_LIBRARY[key]
        cx, cy = tile_center(row, col)
        cx += offset[0]
        cy += offset[1] - data.get("offset_y", 0) - TILE_HEIGHT / 2
        populate_token_visual(self, key, cx, cy, radius)

        self.setZValue(z)

        if data.get("hover_range"):
            self.setAcceptHoverEvents(True)

        # clic gauche : glisser-depose habituel. clic droit : demarre une fleche
        # (uniquement actif en mode Fleche, voir mousePressEvent) ; sinon il passe a
        # travers vers la case en-dessous (efface une couche de couleur en mode Peindre)
        self.setAcceptedMouseButtons(Qt.LeftButton | Qt.RightButton)
        self._hold_timer = QTimer()
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_drag)
    def hoverEnterEvent(self, event):
        radius = TOKEN_LIBRARY[self.key].get("hover_range")
        if radius and self.scene():
            self.scene().highlight_range(self.row, self.col, radius)
        super().hoverEnterEvent(event)
    def hoverLeaveEvent(self, event):
        if TOKEN_LIBRARY[self.key].get("hover_range") and self.scene():
            self.scene().clear_range_highlight()
        super().hoverLeaveEvent(event)
    # Clic droit (mode Fleche uniquement) : demarre le glisser d'une fleche depuis ce
    # token. Clic gauche : demarre le minuteur de hold pour le glisser-depose (position
    # de reference en absolu via scenePos(), pour un suivi fluide sans derive)
    def mousePressEvent(self, event):
        scene = self.scene()

        if event.button() == Qt.RightButton:
            if scene and scene.mode == EditorMode.ARROW:
                self._arrow_dragging = True
                scene.start_arrow_drag(self, event.scenePos())
                event.accept()
            else:
                event.ignore()  # passe a travers vers la case (ex: effacer une couleur)
            return

        if scene and scene.mode != EditorMode.TOKEN:
            scene.set_mode(EditorMode.TOKEN)  # cliquer un token bascule en mode Token
        self._drag_active = False
        self._drag_offset = event.scenePos() - self.scenePos()
        self._hold_timer.start(self.HOLD_MS)
        event.accept()
    def _start_drag(self):
        self._drag_active = True
        self.setZValue(500)
    # Suivi fluide en position absolue (scenePos - offset de prise), plutot qu'en
    # delta cumulatif, pour eviter toute derive pendant un glisser long. Pendant un
    # glisser de fleche, fait suivre l'apercu au curseur a la place.
    def mouseMoveEvent(self, event):
        if self._arrow_dragging:
            scene = self.scene()
            if scene:
                scene.update_arrow_drag(event.scenePos())
            event.accept()
            return
        if self._drag_active:
            new_pos = event.scenePos() - self._drag_offset
            self.setPos(new_pos)
        event.accept()
    # Relachement : si un glisser de fleche etait en cours, la finalise. Sinon, si le
    # drag de token etait actif, on le deplace reellement dans les donnees
    # (scene.move_token) vers la case sous le curseur (ou on le repose a l'origine si
    # hors grille/case invalide). Sinon (simple clic sans hold) : popup de sous-tokens
    # si ce type en a, sinon retrait du token.
    def mouseReleaseEvent(self, event):
        scene = self.scene()

        if event.button() == Qt.RightButton:
            if self._arrow_dragging:
                self._arrow_dragging = False
                if scene:
                    scene.finish_arrow_drag(event.scenePos())
                event.accept()
                return
            event.ignore()
            return

        self._hold_timer.stop()
        if self._drag_active:
            self._drag_active = False
            self.setZValue(self._z_normal)
            if scene:
                target = scene.tile_at(event.scenePos())
                scene.move_token(self, self._origin_key, target if target else self._origin_key)
        else:
            children = TOKEN_LIBRARY[self.key].get("children")
            if children and scene:
                scene.show_token_picker(self.row, self.col, children)
            elif scene:
                scene.remove_specific_token(self._origin_key, self)
        self._drag_offset = None
        event.accept()
#Gere nox
class NoxToken(TokenItem):
    """Totem Nox : une direction (N/E/S/O, tournee au clic droit) et un niveau (1 a 6,
    ajuste a la molette en survolant le token). La zone d'effet affichee au survol
    depend en plus de l'element actuellement selectionne dans la toolbar."""

    DIRECTIONS = ["N", "E", "S", "O"]
    LEVEL_MIN, LEVEL_MAX = 1, 6

    def __init__(self, row, col, direction="S", level=1, offset=(0, 0), z=200):
        super().__init__("nox", row, col, offset=offset, z=z)
        self.direction = direction if direction in self.DIRECTIONS else "S"
        self.level = max(self.LEVEL_MIN, min(self.LEVEL_MAX, level))
        # le totem reagit aussi au clic droit (rotation), contrairement aux autres tokens
        self.setAcceptedMouseButtons(Qt.LeftButton | Qt.RightButton)
        self.setAcceptHoverEvents(True)

    # Clic droit : fait tourner la direction du totem (N -> E -> S -> O -> N) et
    # rafraichit le surlignage si on est en train de le survoler. Clic gauche : le
    # comportement normal de TokenItem (hold pour glisser, sinon retrait)
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            idx = self.DIRECTIONS.index(self.direction)
            self.direction = self.DIRECTIONS[(idx + 1) % len(self.DIRECTIONS)]
            scene = self.scene()
            if scene:
                scene._emit_op({"type": "nox_rotate", "row": self.row, "col": self.col, "direction": self.direction})
            if scene and scene.hovering_nox is self:
                scene.highlight_nox(self)
            event.accept()
            return
        super().mousePressEvent(event)

    # Appele par IsoView.wheelEvent quand la molette tourne au-dessus du totem
    def change_level(self, delta):
        self.level = max(self.LEVEL_MIN, min(self.LEVEL_MAX, self.level + delta))
        scene = self.scene()
        if scene:
            scene._emit_op({"type": "nox_level", "row": self.row, "col": self.col, "level": self.level})
        if scene and scene.hovering_nox is self:
            scene.highlight_nox(self)

    def hoverEnterEvent(self, event):
        scene = self.scene()
        if scene:
            scene.hovering_nox = self
            scene.highlight_nox(self)
        event.accept()

    def hoverLeaveEvent(self, event):
        scene = self.scene()
        if scene:
            scene.clear_range_highlight()
            scene.hovering_nox = None
        event.accept()
#ferme l'affichage des sous token apres un choix
class PickerBackdrop(QGraphicsRectItem):
    def __init__(self, rect):
        super().__init__(rect)
        self.setBrush(QBrush(QColor(0, 0, 0, 60)))
        self.setPen(QPen(Qt.NoPen))
        self.setZValue(900)
        self.setAcceptedMouseButtons(Qt.LeftButton | Qt.RightButton)

    def mousePressEvent(self, event):
        if self.scene():
            self.scene().close_token_picker()
        event.accept()
#gere l'affichage des enfant(invo/mecanismes) d'un token joueur/classe
class PickerIcon(QGraphicsItemGroup):
    """Une icone cliquable du popup de sous-tokens. Au clic, pose le sous-token
    choisi sur la case cible et ferme le popup."""

    RADIUS = 18

    def __init__(self, child_key, cx, cy, target_row, target_col):
        super().__init__()
        self.child_key = child_key
        self.target_row = target_row
        self.target_col = target_col

        bg = QGraphicsEllipseItem(
            cx - self.RADIUS - 3, cy - self.RADIUS - 3, (self.RADIUS + 3) * 2, (self.RADIUS + 3) * 2
        )
        bg.setBrush(QBrush(QColor("white")))
        bg.setPen(QPen(QColor("black")))
        self.addToGroup(bg)

        populate_token_visual(self, child_key, cx, cy, self.RADIUS)

        self.setZValue(950)
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def mousePressEvent(self, event):
        scene = self.scene()
        if scene:
            scene.place_specific_token(self.target_row, self.target_col, self.child_key)
            scene.close_token_picker()
        event.accept()
#gere l'affichage des 6 joueurs et leur initiative
class PlayerPanel(QListWidget):
    """Petit cadre flottant (coin superieur droit de la vue) listant, dans l'ordre de
    jeu, les tokens de personnages joueurs actuellement sur la carte (max MAX_PLAYERS).
    Clic gauche sur une icone la selectionne (surlignee) ; clic gauche sur une autre
    echange leurs places. Clic droit sur une icone : bascule son immunite (petite
    epee dessinee dessus)."""

    ICON_SIZE = 32

    def __init__(self, scene, parent=None):
        super().__init__(parent)
        self.scene_ref = scene
        self._entries_snapshot = []  # index -> entree (donnee d'item = un simple int)
        self.selected_index = None  # index actuellement selectionne, en attente d'un echange
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListWidget.Static)
        self.setSelectionMode(QAbstractItemView.NoSelection)  # selection geree a la main (surlignage manuel)
        self.setDragDropMode(
            QAbstractItemView.NoDragDrop)  # pas de glisser-depose natif : source de bugs, remplace par clic/clic
        self.setIconSize(QSize(self.ICON_SIZE, self.ICON_SIZE))
        self.setSpacing(4)
        self.setFixedSize((MAX_PLAYERS + 1) * (self.ICON_SIZE + 8) + 10, self.ICON_SIZE + 22)
        self.setStyleSheet(
            "QListWidget { background-color: rgba(20,20,25,190); border: 1px solid #555; border-radius: 6px; }"
            "QListWidget::item { margin: 2px; }"
        )

    # Reconstruit entierement la liste affichee a partir de scene.player_order
    def refresh(self):
        self.clear()
        self._entries_snapshot = list(self.scene_ref.player_order)
        for i, entry in enumerate(self._entries_snapshot):
            item = QListWidgetItem()
            item.setIcon(self._make_icon(entry))
            item.setData(Qt.UserRole, i)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if i == self.selected_index:
                item.setBackground(QBrush(QColor(255, 255, 255, 90)))
            self.addItem(item)

    # Construit l'icone d'un joueur : son image de token, avec une petite epee
    # dessinee dessus s'il a l'immunite
    def _make_icon(self, entry):
        data = TOKEN_LIBRARY[entry["type"]]
        path = os.path.join(ASSETS_DIR, data["image"])
        pixmap = QPixmap(path)
        if pixmap.isNull():
            pixmap = QPixmap(self.ICON_SIZE, self.ICON_SIZE)
            pixmap.fill(data["color"])
        else:
            pixmap = pixmap.scaled(
                self.ICON_SIZE, self.ICON_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        if entry.get("state", {}).get("immune"):
            pixmap = self._with_sword_badge(pixmap)
        return QIcon(pixmap)

    # Dessine une petite epee (lame + garde) dans le coin de l'icone : pas d'asset
    # dedie, juste deux traits au QPainter
    def _with_sword_badge(self, pixmap):
        composed = QPixmap(pixmap.size())
        composed.fill(Qt.transparent)
        painter = QPainter(composed)
        painter.drawPixmap(0, 0, pixmap)
        w, h = pixmap.width(), pixmap.height()
        pen = QPen(QColor("white"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(w - 11, h - 1, w - 1, h - 11)  # lame
        painter.drawLine(w - 9, h - 5, w - 5, h - 9)  # garde
        painter.end()
        return composed

    # Clic gauche : premiere icone cliquee = selectionnee (surlignee) ; clic sur une
    # deuxieme = echange leurs deux places dans player_order. Clic droit : assigne
    # l'immunite a l'icone cliquee et la retire de toutes les autres (une seule epee
    # possible en meme temps) ; reclique sur celle qui l'a deja = la lui retire.
    def mousePressEvent(self, event):
        item = self.itemAt(event.pos())

        if event.button() == Qt.RightButton:
            if item:
                idx = item.data(Qt.UserRole)
                if idx is not None and 0 <= idx < len(self._entries_snapshot):
                    target_entry = self._entries_snapshot[idx]
                    was_immune = target_entry.get("state", {}).get("immune", False)
                    for e in self.scene_ref.player_order:
                        e.setdefault("state", {})["immune"] = False
                    if not was_immune:
                        target_entry.setdefault("state", {})["immune"] = True
                    self.refresh()
            return

        if event.button() == Qt.LeftButton:
            if not item:
                self.selected_index = None
                self.refresh()
                return

            idx = item.data(Qt.UserRole)
            if idx is None:
                return

            if self.selected_index is None:
                self.selected_index = idx
            elif idx == self.selected_index:
                self.selected_index = None  # reclique sur la meme -> annule la selection
            else:
                order = self.scene_ref.player_order
                order[self.selected_index], order[idx] = order[idx], order[self.selected_index]
                self.selected_index = None
            self.refresh()
            return

        super().mousePressEvent(event)
#gere les couleur, le picker, les recents etc
class ColorSwatchButton(QPushButton):
    """Petit bouton carre affichant une couleur, avec un liseret blanc au survol."""

    def __init__(self, color, size=24, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.set_color(color)

    def set_color(self, color):
        self.setStyleSheet(
            f"QPushButton {{ background-color: {color.name()}; border: 1px solid black; border-radius: 3px; }}"
            f"QPushButton:hover {{ border: 2px solid white; }}"
        )
        self.setToolTip(color.name())
#Quelques fonctions de logiques que je devrais ranger
def isBorder(row, col):
    return row == 0 or col == 0 or row == ROWS - 1 or col == COLS - 1
def isCorner(row, col):
    return row in [0, ROWS - 1] and col in [0, COLS - 1]
def iso_to_screen(row, col):
    x = (col - row) * (TILE_WIDTH // 2)
    y = (col + row) * (TILE_HEIGHT // 2)
    return x, y
def tile_center(row, col):
    x, y = iso_to_screen(row, col)
    return x + TILE_WIDTH / 2, y + TILE_HEIGHT / 2
def add_image(scene, row, col, image_path):
    multiplier = 2 if image_path == "cube.png" else 1
    target_size = 96 * multiplier
    pixmap = QPixmap(image_path)
    if pixmap.isNull():
        return

    # meme principe que populate_token_visual : on garde le pixmap a sa resolution
    # native et on applique un facteur d'echelle sur l'item, au lieu de rapetisser le
    # bitmap une fois pour toutes (ce qui figeait sa nettete au zoom)
    scale_factor = min(target_size / pixmap.width(), target_size / pixmap.height())

    item = QGraphicsPixmapItem(pixmap)
    item.setTransformationMode(Qt.SmoothTransformation)
    item.setScale(scale_factor)

    w = pixmap.width() * scale_factor
    h = pixmap.height() * scale_factor
    x, y = iso_to_screen(row, col)
    item.setPos(
        x + TILE_WIDTH / 2 - w / 2,
        y + TILE_HEIGHT / 2 - h - 2.5,
    )
    item.setZValue(row + col + 10)
    scene.addItem(item)
def is_pile(row, col):
    return (
            (row == 4 and col == 4)
            or (row == 11 and col == 3)
            or (row == 3 and col == 12)
            or (row == 9 and col == 16)
            or (row == 14 and col == 10)
    )
#gere les cases de la vue
class IsoTile(QGraphicsPolygonItem):
    def __init__(self, row, col):
        super().__init__()
        self.row = row
        self.col = col
        self.color_layers = []

        colorborder = QColor("lightblue")
        w, h = TILE_WIDTH, TILE_HEIGHT

        polygon = QPolygonF(
            [
                QPointF(0, h / 2),
                QPointF(w / 2, 0),
                QPointF(w, h / 2),
                QPointF(w / 2, h),
            ]
        )
        self.setPolygon(polygon)
        self.setPen(QPen(QColor("black")))

        if (row + col) % 2:
            self.default_brush = QBrush(QColor("#4a4a4a"))
        else:
            self.default_brush = QBrush(QColor("#2d2d2d"))

        if isBorder(row, col):
            self.default_brush = QBrush(QColor("black")) if isCorner(row, col) else QBrush(colorborder)

        self.hover_brush = QBrush(QColor(100, 120, 160))
        self.range_highlight = False  # False, ou un QColor (couleur d'element) si surligne
        self.setBrush(self.default_brush)
        self.setAcceptHoverEvents(True)
        self.setZValue(row + col)

    def base_brush(self):
        return QBrush(self.color_layers[-1]) if self.color_layers else self.default_brush

    def current_display_brush(self):
        if isinstance(self.range_highlight, QColor):
            return QBrush(self.range_highlight)
        return self.base_brush()

    def set_highlight(self, active_or_color):
        # Recoit soit False (pour eteindre), soit un QColor (couleur d'element)
        self.range_highlight = active_or_color
        self.setBrush(self.current_display_brush())

    def hoverEnterEvent(self, event):
        buttons = QApplication.mouseButtons()
        scene = self.scene()
        mode = scene.mode
        key = (self.row, self.col)
        if buttons & Qt.LeftButton and mode in (EditorMode.PAINT, EditorMode.ERASE):
            if key not in scene.drag_visited:
                scene.drag_visited.add(key)
                self.activate(Qt.LeftButton)
        elif buttons & Qt.RightButton and mode == EditorMode.PAINT:
            if key not in scene.drag_visited:
                scene.drag_visited.add(key)
                self.reset_color()
        else:
            self.setBrush(self.hover_brush)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(self.current_display_brush())
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        scene = self.scene()
        key = (self.row, self.col)
        if event.button() == Qt.RightButton:
            if scene.mode == EditorMode.PAINT:
                scene.drag_visited.add(key)
                self.reset_color()
            else:
                scene.erase_at(self.row, self.col)
        else:
            if scene.mode in (EditorMode.PAINT, EditorMode.ERASE):
                scene.drag_visited.add(key)
            self.activate(event.button())
        event.accept()

    def mouseMoveEvent(self, event):
        scene = self.scene()
        mode = scene.mode
        if mode not in (EditorMode.PAINT, EditorMode.ERASE):
            event.accept()
            return

        target_key = scene.tile_at(event.scenePos())
        if target_key is None:
            event.accept()
            return
        target_tile = scene.tiles.get(target_key)
        if target_tile is None:
            event.accept()
            return

        buttons = event.buttons()
        if buttons & Qt.LeftButton and mode in (EditorMode.PAINT, EditorMode.ERASE):
            if target_key not in scene.drag_visited:
                scene.drag_visited.add(target_key)
                target_tile.activate(Qt.LeftButton)
        elif buttons & Qt.RightButton and mode == EditorMode.PAINT:
            if target_key not in scene.drag_visited:
                scene.drag_visited.add(target_key)
                target_tile.reset_color()

        event.accept()

    def reset_color(self):
        if not self.color_layers:
            return
        scene = self.scene()
        scene.push_undo_snapshot()
        self.color_layers.pop()
        self.setBrush(self.current_display_brush())
        scene._emit_op({"type": "erase_tile_color", "row": self.row, "col": self.col})

    def activate(self, button):
        scene = self.scene()
        mode = scene.mode

        if mode == EditorMode.PAINT and button == Qt.LeftButton:
            scene.push_undo_snapshot()
            self.color_layers.append(QColor(scene.current_color))
            self.setBrush(self.current_display_brush())
            scene._emit_op({"type": "paint_tile", "row": self.row, "col": self.col,
                            "color": scene.current_color.name()})

        elif mode == EditorMode.TOKEN and button == Qt.LeftButton:
            scene.toggle_token(self.row, self.col)

        elif mode == EditorMode.ERASE and button == Qt.LeftButton:
            scene.erase_at(self.row, self.col)

        print(f"Tile {self.row},{self.col} - mode {mode.name}")
#gere l'etat de la map, tile + tokens + fleches + undo/redo
class MapScene(QGraphicsScene):
    def __init__(self):
        super().__init__()
        self.mode = EditorMode.PAINT
        self.current_color = QColor("crimson")
        self.recent_colors = []
        self.current_token_key = next(iter(TOKEN_LIBRARY))

        self.tiles = {}
        self.tokens = {}
        self.arrows = []
        self.arrow_drag = None  # {"start_key":..., "color":..., "line":...} pendant un glisser de fleche en cours
        self.rail_items = []
        self.range_highlighted = []
        self.highlight_overlays = []  # items translucides empilables (ex: anneaux de joueurs qui se recouvrent)

        self.undo_stack = []
        self.redo_stack = []
        self.max_history = 50

        self.drag_visited = set()
        self.main_window = None
        self.active_picker = None
        self.nox_element = "eau"  # element actuellement selectionne pour le hover du Nox
        self.hovering_nox = None  # instance de NoxToken actuellement survolee
        self.player_order = []  # references directes vers les entrees de tokens "player", dans l'ordre de jeu (max MAX_PLAYERS)
        self._applying_remote_op = False
        self.on_local_op = None
    def apply_remote_op(self, op):
        self._applying_remote_op = True
        try:
            kind = op["type"]
            if kind == "paint_tile":
                tile = self.tiles.get((op["row"], op["col"]))
                if tile:
                    tile.color_layers.append(QColor(op["color"]))
                    tile.setBrush(tile.current_display_brush())
            elif kind == "erase_tile_color":
                tile = self.tiles.get((op["row"], op["col"]))
                if tile:
                    tile.reset_color()
            elif kind == "toggle_token":
                self._place_or_toggle(op["row"], op["col"], op["token_type"])
            elif kind == "remove_specific_token":
                entries = self.tokens.get((op["row"], op["col"]))
                if entries:
                    entry = next((e for e in entries if e["type"] == op["token_type"]), None)
                    if entry:
                        self._remove_token_entry((op["row"], op["col"]), entry)
                        self.update_rails()
            elif kind == "move_token":
                entries = self.tokens.get((op["origin_row"], op["origin_col"]))
                if entries:
                    entry = next((e for e in entries if e["type"] == op["token_type"]), None)
                    if entry:
                        self.move_token(entry["item"], (op["origin_row"], op["origin_col"]),
                                        (op["target_row"], op["target_col"]))
            elif kind == "place_specific_token":
                self.place_specific_token(op["row"], op["col"], op["token_type"])
            elif kind == "add_arrow":
                self.add_arrow(tuple(op["start"]), tuple(op["end"]), QColor(op["color"]))
            elif kind == "erase_cell":
                self.erase_at(op["row"], op["col"])
            elif kind == "nox_rotate":
                entries = self.tokens.get((op["row"], op["col"]))
                if entries:
                    entry = next((e for e in entries if e["type"] == "nox"), None)
                    if entry and isinstance(entry["item"], NoxToken):
                        entry["item"].direction = op["direction"]
            elif kind == "nox_level":
                entries = self.tokens.get((op["row"], op["col"]))
                if entries:
                    entry = next((e for e in entries if e["type"] == "nox"), None)
                    if entry and isinstance(entry["item"], NoxToken):
                        entry["item"].level = op["level"]
            elif kind == "clear_all":
                self.apply_remote_clear_all()
        finally:
            self._applying_remote_op = False
    def apply_remote_clear_all(self):
        for entries in list(self.tokens.values()):
            for entry in list(entries):
                self.removeItem(entry["item"])
        self.tokens.clear()
        for arrow in self.arrows:
            self.removeItem(arrow["item"])
        self.arrows.clear()
        for item in self.rail_items:
            self.removeItem(item)
        self.rail_items.clear()
        for tile in self.tiles.values():
            tile.color_layers = []
            tile.setBrush(tile.current_display_brush())
        self.active_picker = None
        self.hovering_nox = None
        self.player_order = []
        if self.main_window:
            self.main_window.refresh_player_panel()
    def _emit_op(self, op):
        if self._applying_remote_op:
            return
        if self.on_local_op:
            self.on_local_op(op)
    def set_mode(self, mode):
        self.mode = mode
        if self.main_window:
            action = self.main_window.mode_actions.get(mode)
            if action:
                action.setChecked(True)
    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.drag_visited.clear()
    def tile_at(self, scene_pos):
        for it in self.items(scene_pos):
            if isinstance(it, IsoTile):
                return (it.row, it.col)
        return None
    def add_recent_color(self, color):
        hex_val = color.name()
        self.recent_colors = [c for c in self.recent_colors if c.name() != hex_val]
        self.recent_colors.insert(0, QColor(color))
        self.recent_colors = self.recent_colors[:6]
    @staticmethod
    def _stack_offset(index):
        return (index * 18, -index * 14)
    def _add_token_entry(self, row, col, token_type, state=None):
        key = (row, col)
        entries = self.tokens.setdefault(key, [])
        entries.append({"item": None, "type": token_type, "state": state or {}})
        self._restack(key)
        return entries[-1]
    # Cree le bon type d'item pour une entree de token : NoxToken (avec sa direction/
    # son niveau restaures) pour "nox", TokenItem classique sinon
    def _make_token_item(self, token_type, row, col, offset, state):
        if token_type == "nox":
            return NoxToken(
                row, col,
                direction=state.get("direction", "S"),
                level=state.get("level", 1),
                offset=offset,
            )
        return TokenItem(token_type, row, col, offset=offset)
    def _restack(self, key):
        entries = self.tokens.get(key)
        if not entries:
            return
        row, col = key
        specs = [(e["type"], e.get("state", {})) for e in entries]
        # avant de reconstruire, on repere quelles entrees actuelles sont referencees
        # par player_order (par leur index dans cette liste), pour pouvoir reconnecter
        # player_order vers les nouvelles instances apres reconstruction ci-dessous
        old_index_by_id = {id(e): i for i, e in enumerate(entries)}
        player_order_links = [
            (po_i, old_index_by_id[id(e)])
            for po_i, e in enumerate(self.player_order)
            if id(e) in old_index_by_id
        ]
        for e in entries:
            if e["item"] is not None:
                self.removeItem(e["item"])
        entries.clear()
        new_entries = []
        for i, (token_type, state) in enumerate(specs):
            item = self._make_token_item(token_type, row, col, self._stack_offset(i), state)
            self.addItem(item)
            new_entry = {"item": item, "type": token_type, "state": state}
            entries.append(new_entry)
            new_entries.append(new_entry)
        for po_i, old_i in player_order_links:
            self.player_order[po_i] = new_entries[old_i]
    def _remove_token_entry(self, key, entry):
        entries = self.tokens.get(key)
        if not entries or entry not in entries:
            return
        self.removeItem(entry["item"])
        entries.remove(entry)
        if entry in self.player_order:
            self.player_order.remove(entry)
            if self.main_window:
                self.main_window.refresh_player_panel()
        if entries:
            self._restack(key)
        else:
            del self.tokens[key]
    def capture_snapshot(self):
        entry_location = {
            id(e): (pos, i)
            for pos, entries in self.tokens.items()
            for i, e in enumerate(entries)
        }
        return {
            "tiles": {
                pos: [QColor(c) for c in tile.color_layers]
                for pos, tile in self.tiles.items()
                if tile.color_layers
            },
            "tokens": {
                pos: [
                    {
                        "type": e["type"],
                        "state": (
                            {"direction": e["item"].direction, "level": e["item"].level}
                            if isinstance(e["item"], NoxToken) else dict(e.get("state", {}))
                        ),
                    }
                    for e in entries
                ]
                for pos, entries in self.tokens.items()
            },
            "arrows": [(a["start"], a["end"], QColor(a["color"])) for a in self.arrows],
            "player_order": [entry_location[id(e)] for e in self.player_order if id(e) in entry_location],
        }
    def restore_snapshot(self, snapshot):
        for entries in self.tokens.values():
            for entry in entries:
                self.removeItem(entry["item"])
        self.tokens.clear()

        for arrow in self.arrows:
            self.removeItem(arrow["item"])
        self.arrows.clear()

        for item in self.rail_items:
            self.removeItem(item)
        self.rail_items.clear()

        for tile in self.tiles.values():
            tile.color_layers = []
            tile.setBrush(tile.current_display_brush())

        for pos, colors in snapshot["tiles"].items():
            tile = self.tiles.get(pos)
            if tile:
                tile.color_layers = [QColor(c) for c in colors]
                tile.setBrush(tile.current_display_brush())

        for pos, specs in snapshot["tokens"].items():
            for spec in specs:
                self._add_token_entry(pos[0], pos[1], spec["type"], spec.get("state"))

        for start, end, color in snapshot["arrows"]:
            self.add_arrow(start, end, color)

        self.player_order = []
        for pos, idx in snapshot.get("player_order", []):
            entries = self.tokens.get(pos)
            if entries and idx < len(entries):
                self.player_order.append(entries[idx])
        if self.main_window:
            self.main_window.refresh_player_panel()

        self.hovering_nox = None
        self.update_rails()
    def push_undo_snapshot(self):
        if self._applying_remote_op:
            return
        self.undo_stack.append(self.capture_snapshot())
        if len(self.undo_stack) > self.max_history:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
    def undo(self):
        if not self.undo_stack:
            return
        current = self.capture_snapshot()
        snapshot = self.undo_stack.pop()
        self.redo_stack.append(current)
        self.restore_snapshot(snapshot)
    def redo(self):
        if not self.redo_stack:
            return
        current = self.capture_snapshot()
        snapshot = self.redo_stack.pop()
        self.undo_stack.append(current)
        self.restore_snapshot(snapshot)
    def toggle_token(self, row, col):
        self._place_or_toggle(row, col, self.current_token_key)
        self._emit_op({"type": "toggle_token", "row": row, "col": col, "token_type": self.current_token_key})
    # Pose ou retire un type de token precis sur une case. Comportement selon la
    # categorie du type (voir TOKEN_LIBRARY["category"]) :
    #  - "player" (les 18 classes) : jamais unique (doublons permis), mais plafonne a
    #    MAX_PLAYERS au total sur la carte ; chaque pose est enregistree dans
    #    player_order (voir le panneau de joueurs)
    #  - autres types marques "unique" (cadran, lapin, nox) : un seul exemplaire sur
    #    toute la carte, le reposer ailleurs le deplace (retire l'ancien d'abord)
    #  - le reste (invocations Steamer/Sadida) : ni unique ni plafonne, s'empile
    # Dans tous les cas, cliquer sur la case ou ce type est deja present le retire
    # (toggle off).
    def _place_or_toggle(self, row, col, token_type):
        data = TOKEN_LIBRARY[token_type]
        category = data.get("category")
        key = (row, col)

        entries_here = self.tokens.get(key, [])
        existing_here = next((e for e in entries_here if e["type"] == token_type), None)
        if existing_here:
            self.push_undo_snapshot()
            self._remove_token_entry(key, existing_here)
            self.update_rails()
            return

        if category == "player":
            total_players = sum(
                1 for entries in self.tokens.values() for e in entries
                if TOKEN_LIBRARY[e["type"]].get("category") == "player"
            )
            if total_players >= MAX_PLAYERS:
                return  # limite de joueurs atteinte : on ignore le clic
            self.push_undo_snapshot()
            entry = self._add_token_entry(row, col, token_type)
            self.player_order.append(entry)
            if self.main_window:
                self.main_window.refresh_player_panel()
            self.update_rails()
            return

        self.push_undo_snapshot()
        if data.get("unique"):
            for pos, entries in list(self.tokens.items()):
                match = next((e for e in entries if e["type"] == token_type), None)
                if match:
                    self._remove_token_entry(pos, match)

        self._add_token_entry(row, col, token_type)
        self.update_rails()
    def show_token_picker(self, row, col, children_keys):
        self.close_token_picker()

        backdrop = PickerBackdrop(self.sceneRect())
        self.addItem(backdrop)

        cx, cy = tile_center(row, col)
        spacing = 46
        top_y = cy - TILE_HEIGHT / 2 - 55
        start_x = cx - (len(children_keys) - 1) * spacing / 2

        icons = []
        for i, child_key in enumerate(children_keys):
            icon = PickerIcon(child_key, start_x + i * spacing, top_y, row, col)
            self.addItem(icon)
            icons.append(icon)

        self.active_picker = {"backdrop": backdrop, "icons": icons}
    def close_token_picker(self):
        if not self.active_picker:
            return
        self.removeItem(self.active_picker["backdrop"])
        for icon in self.active_picker["icons"]:
            self.removeItem(icon)
        self.active_picker = None
    def place_specific_token(self, row, col, token_type):
        self.push_undo_snapshot()
        self._add_token_entry(row, col, token_type)
        self.update_rails()
        self._emit_op({"type": "place_specific_token", "row": row, "col": col, "token_type": token_type})
    def move_token(self, token_item, origin_key, target_key):
        origin_entries = self.tokens.get(origin_key)
        if not origin_entries:
            return
        entry = next((e for e in origin_entries if e["item"] is token_item), None)
        if entry is None:
            return
        token_type = entry["type"]  # capture avant mutation, pour l'emission plus bas

        if target_key == origin_key:
            self._restack(origin_key)
            return

        target_entries = self.tokens.get(target_key, [])
        if any(e["type"] == entry["type"] for e in target_entries):
            self._restack(origin_key)
            return

        state = dict(entry.get("state", {}))
        if isinstance(token_item, NoxToken):
            state = {"direction": token_item.direction, "level": token_item.level}

        was_in_player_order = entry in self.player_order
        player_order_idx = self.player_order.index(entry) if was_in_player_order else None

        self.push_undo_snapshot()

        origin_entries.remove(entry)
        if not origin_entries:
            del self.tokens[origin_key]

        self.removeItem(token_item)
        self.tokens.setdefault(target_key, []).append({"item": None, "type": entry["type"], "state": state})

        if origin_key in self.tokens:
            self._restack(origin_key)
        self._restack(target_key)

        if player_order_idx is not None:
            self.player_order[player_order_idx] = self.tokens[target_key][-1]

        self.update_rails()
        self._emit_op({
            "type": "move_token",
            "origin_row": origin_key[0], "origin_col": origin_key[1],
            "target_row": target_key[0], "target_col": target_key[1],
            "token_type": token_type,
        })
    def remove_specific_token(self, key, item):
        entries = self.tokens.get(key)
        if not entries:
            return
        entry = next((e for e in entries if e["item"] is item), None)
        if entry is None:
            return
        self.push_undo_snapshot()
        self._remove_token_entry(key, entry)
        self.update_rails()
        self._emit_op({"type": "remove_specific_token", "row": key[0], "col": key[1], "token_type": entry["type"]})
    def highlight_range(self, row, col, radius, element="default"):
        color = ELEMENT_COLORS.get(element, ELEMENT_COLORS["default"])
        self.clear_range_highlight()
        for (r, c), tile in self.tiles.items():
            if abs(r - row) + abs(c - col) <= radius:
                tile.set_highlight(color)
                self.range_highlighted.append((r, c))
    # Calcule et surligne la zone d'effet du totem Nox survole, selon l'element
    # actuellement selectionne dans la toolbar (eau/air/feu/terre), sa direction et
    # son niveau
    def highlight_nox(self, nox_item):
        self.clear_range_highlight()
        apex = (nox_item.row, nox_item.col)
        level = nox_item.level
        direction = nox_item.direction
        element = self.nox_element
        color = ELEMENT_COLORS.get(element, ELEMENT_COLORS["default"])

        cells = set()
        if element == "eau":
            # cone double (direction + direction opposee), depth = niveau+1
            depth = level + 1
            cells |= cone_cells(apex, direction, depth)
            cells |= cone_cells(apex, OPPOSITE_DIRECTION[direction], depth)
        elif element == "terre":
            # cone simple, base centree sur Nox (voir terre_cone_cells), depth = niveau+2
            depth = level + 2
            cells |= terre_cone_cells(apex, direction, depth)
        elif element == "air":
            # zone de base autour de Nox : anneau 1-4 + tout au-dela de 7, toujours
            # affichee quel que soit le niveau (reste dans `cells`, couleur "air" habituelle)
            for pos in self.tiles:
                d = abs(pos[0] - apex[0]) + abs(pos[1] - apex[1])
                if (1 <= d <= 4) or d > 7:
                    cells.add(pos)
            # + cumulatif selon le niveau : les N premiers joueurs de l'ordre du
            # panneau (N = niveau ; lv3 -> les 3 premiers), chacun avec son propre
            # anneau 1-4 autour de sa position actuelle, sauf s'il a l'immunite.
            # Rose a 50% d'opacite, en overlay separe (pas via `cells`/tile.set_highlight)
            # pour que les anneaux qui se recouvrent s'assombrissent visiblement au lieu
            # de simplement s'ecraser l'un l'autre.
            player_ring_color = QColor(255, 105, 180, 127)
            for i in range(min(level, len(self.player_order))):
                entry = self.player_order[i]
                if entry.get("state", {}).get("immune"):
                    continue
                item = entry.get("item")
                if item is None:
                    continue
                prow, pcol = item.row, item.col
                for pos in self.tiles:
                    d = abs(pos[0] - prow) + abs(pos[1] - pcol)
                    if 1 <= d <= 4:
                        self._add_ring_overlay(pos[0], pos[1], player_ring_color)
        elif element == "feu":
            # tout ce qui est a moins de (niveau+2) cases : le rayon grandit avec le niveau
            radius = level + 2
            for pos in self.tiles:
                if abs(pos[0] - apex[0]) + abs(pos[1] - apex[1]) < radius:
                    cells.add(pos)

        cells.discard(apex)
        for pos in cells:
            tile = self.tiles.get(pos)
            if tile:
                tile.set_highlight(color)
                self.range_highlighted.append(pos)
    def clear_range_highlight(self):
        for pos in self.range_highlighted:
            tile = self.tiles.get(pos)
            if tile:
                tile.set_highlight(False)
        self.range_highlighted = []

        for item in self.highlight_overlays:
            self.removeItem(item)
        self.highlight_overlays = []
    # Ajoute un losange translucide independant sur une case (au lieu de teinter la
    # case elle-meme) : plusieurs overlays sur la meme case s'empilent visuellement
    # au lieu de s'ecraser l'un l'autre (utilise pour les anneaux de joueurs qui
    # peuvent se recouvrir)
    def _add_ring_overlay(self, row, col, color):
        x, y = iso_to_screen(row, col)
        w, h = TILE_WIDTH, TILE_HEIGHT
        polygon = QPolygonF(
            [QPointF(x, y + h / 2), QPointF(x + w / 2, y), QPointF(x + w, y + h / 2), QPointF(x + w / 2, y + h)]
        )
        overlay = QGraphicsPolygonItem(polygon)
        overlay.setBrush(QBrush(color))
        overlay.setPen(QPen(Qt.NoPen))
        overlay.setZValue(60)  # au-dessus des cases/etiquettes, en-dessous des tokens
        self.addItem(overlay)
        self.highlight_overlays.append(overlay)
    def update_rails(self):
        for item in self.rail_items:
            self.removeItem(item)
        self.rail_items = []

        microbots = [
            pos for pos, entries in self.tokens.items()
            if any(e["type"] == "steamer_microbot" for e in entries)
        ]
        if len(microbots) < 2:
            return

        by_row = defaultdict(list)
        by_col = defaultdict(list)
        for r, c in microbots:
            by_row[r].append(c)
            by_col[c].append(r)

        def link_row(fixed, values):
            values = sorted(values)
            for a, b in zip(values, values[1:]):
                if b - a < 2:
                    continue
                span = b - a + 1
                if span > RAIL_MAX_LENGTH:
                    continue
                for v in range(a + 1, b):
                    self._place_rail_token(fixed, v)

        def link_col(fixed, values):
            values = sorted(values)
            for a, b in zip(values, values[1:]):
                if b - a < 2:
                    continue
                span = b - a + 1
                if span > RAIL_MAX_LENGTH:
                    continue
                for v in range(a + 1, b):
                    self._place_rail_token(v, fixed)

        for row, cols in by_row.items():
            link_row(row, cols)
        for col, rows in by_col.items():
            link_col(col, rows)
    def _place_rail_token(self, row, col):
        for existing in self.rail_items:
            if isinstance(existing, TokenItem) and (existing.row, existing.col) == (row, col):
                return
        badge = TokenItem("steamer_rail", row, col, radius=16, z=140)
        self.addItem(badge)
        self.rail_items.append(badge)
    # Trouve la couleur de fleche associee a un token, selon sa position dans le
    # panneau de joueurs (1er -> bleu, 2e -> rose, etc.). Couleur neutre par defaut
    # pour les tokens hors panneau (mecanismes, Nox).
    def arrow_color_for_token(self, token_item):
        for i, entry in enumerate(self.player_order):
            if entry.get("item") is token_item and i < len(ARROW_COLORS):
                return ARROW_COLORS[i]
        return ARROW_DEFAULT_COLOR
    # Demarre le glisser d'une fleche depuis un token (clic droit maintenu, voir
    # TokenItem.mousePressEvent) : cree une ligne d'apercu qui suivra le curseur
    def start_arrow_drag(self, token_item, scene_pos):
        color = self.arrow_color_for_token(token_item)
        x1, y1 = tile_center(token_item.row, token_item.col)
        line = QGraphicsLineItem(x1, y1, scene_pos.x(), scene_pos.y())
        line.setPen(QPen(color, 3, Qt.DashLine))
        line.setZValue(160)
        self.addItem(line)
        self.arrow_drag = {
            "start_key": (token_item.row, token_item.col),
            "color": color,
            "line": line,
        }
    # Fait suivre l'apercu de fleche au curseur pendant le glisser
    def update_arrow_drag(self, scene_pos):
        if not self.arrow_drag:
            return
        p1 = self.arrow_drag["line"].line().p1()
        self.arrow_drag["line"].setLine(p1.x(), p1.y(), scene_pos.x(), scene_pos.y())
    # Relachement du clic droit : pose la fleche definitive si on est retombe sur une
    # case valide et differente du depart, sinon annule simplement l'apercu
    def finish_arrow_drag(self, scene_pos):
        if not self.arrow_drag:
            return
        drag = self.arrow_drag
        self.removeItem(drag["line"])
        self.arrow_drag = None

        target_key = self.tile_at(scene_pos)
        if target_key and target_key != drag["start_key"]:
            self.push_undo_snapshot()
            self.add_arrow(drag["start_key"], target_key, drag["color"])
            self._emit_op({"type": "add_arrow", "start": list(drag["start_key"]),
                           "end": list(target_key), "color": drag["color"].name()})
    def add_arrow(self, start, end, color=None):
        color = color or ARROW_DEFAULT_COLOR
        x1, y1 = tile_center(*start)
        x2, y2 = tile_center(*end)

        group = QGraphicsItemGroup()

        line = QGraphicsLineItem(x1, y1, x2, y2)
        line.setPen(QPen(color, 3))
        group.addToGroup(line)

        angle = math.atan2(y2 - y1, x2 - x1)
        head_len = 10
        head_angle = math.radians(28)
        p1 = QPointF(
            x2 - head_len * math.cos(angle - head_angle),
            y2 - head_len * math.sin(angle - head_angle),
        )
        p2 = QPointF(
            x2 - head_len * math.cos(angle + head_angle),
            y2 - head_len * math.sin(angle + head_angle),
        )
        head = QGraphicsPolygonItem(QPolygonF([QPointF(x2, y2), p1, p2]))
        head.setBrush(QBrush(color))
        head.setPen(QPen(color))
        group.addToGroup(head)

        group.setZValue(150)
        self.addItem(group)
        self.arrows.append({"start": start, "end": end, "item": group, "color": color})
    def erase_at(self, row, col):
        self.push_undo_snapshot()
        key = (row, col)

        if key in self.tokens and self.tokens[key]:
            self._remove_token_entry(key, self.tokens[key][-1])
            self.update_rails()

        remaining = []
        for arrow in self.arrows:
            if arrow["start"] == key or arrow["end"] == key:
                self.removeItem(arrow["item"])
            else:
                remaining.append(arrow)
        self.arrows = remaining

        tile = self.tiles.get(key)
        if tile:
            tile.color_layers = []
            tile.setBrush(tile.current_display_brush())

        self._emit_op({"type": "erase_cell", "row": row, "col": col})
    # Serialise tout l'etat modifiable de la carte (couleurs, tokens, fleches, ordre
    # des joueurs, element Nox selectionne) en un dict JSON-compatible, pour
    # l'export/la sauvegarde sur disque
    def to_dict(self):
        entry_location = {
            id(e): (pos, i)
            for pos, entries in self.tokens.items()
            for i, e in enumerate(entries)
        }
        return {
            "version": 1,
            "tiles": {
                f"{pos[0]},{pos[1]}": [c.name(QColor.HexArgb) for c in tile.color_layers]
                for pos, tile in self.tiles.items()
                if tile.color_layers
            },
            "tokens": {
                f"{pos[0]},{pos[1]}": [
                    {
                        "type": e["type"],
                        "state": (
                            {"direction": e["item"].direction, "level": e["item"].level}
                            if isinstance(e["item"], NoxToken) else dict(e.get("state", {}))
                        ),
                    }
                    for e in entries
                ]
                for pos, entries in self.tokens.items()
            },
            "arrows": [
                {"start": list(a["start"]), "end": list(a["end"]), "color": a["color"].name(QColor.HexArgb)}
                for a in self.arrows
            ],
            "player_order": [
                [pos[0], pos[1], idx]
                for pos, idx in (entry_location[id(e)] for e in self.player_order if id(e) in entry_location)
            ],
            "nox_element": self.nox_element,
        }
    # Reconstruit l'etat de la carte a partir d'un dict issu de to_dict (import/
    # ouverture d'un fichier). A appeler sur une scene deja videe et regrillee
    # (voir MainWindow.import_map), pas besoin de push_undo_snapshot ici.
    def load_dict(self, data):
        for key, colors in data.get("tiles", {}).items():
            row, col = (int(v) for v in key.split(","))
            tile = self.tiles.get((row, col))
            if tile:
                tile.color_layers = [QColor(c) for c in colors]
                tile.setBrush(tile.current_display_brush())

        for key, entries in data.get("tokens", {}).items():
            row, col = (int(v) for v in key.split(","))
            for spec in entries:
                self._add_token_entry(row, col, spec["type"], spec.get("state"))

        for a in data.get("arrows", []):
            self.add_arrow(tuple(a["start"]), tuple(a["end"]), QColor(a["color"]))

        self.player_order = []
        for row, col, idx in data.get("player_order", []):
            entries = self.tokens.get((row, col))
            if entries and idx < len(entries):
                self.player_order.append(entries[idx])
        if self.main_window:
            self.main_window.refresh_player_panel()

        self.nox_element = data.get("nox_element", "eau")
        if self.main_window:
            self.main_window.sync_nox_element_button()

        self.update_rails()
    def clear_all(self):
        self.clear()
        self.tiles.clear()
        self.tokens.clear()
        self.arrows.clear()
        self.rail_items.clear()
        self.arrow_drag = None  # self.clear() a deja supprime la ligne d'apercu eventuelle
        self.active_picker = None
        self.hovering_nox = None
        self.player_order = []
        if self.main_window:
            self.main_window.refresh_player_panel()
#gere la vue graphique qui affiche la carte isometrique et gere le zoom/pan
class IsoView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.scene = MapScene()
        self.setScene(self.scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)

        self.scene.setBackgroundBrush(QBrush(QColor("#0f1319")))
        self.setStyleSheet("background-color: #1a1a1a; border: none;")

        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.main_window = None
        self.draw_map()

    def draw_map(self):
        cols = ["Placeholder", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R",
                "S"]
        rows = ["Placeholder", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "",
                ""]
        font = QFont("Consolas", 11, QFont.Bold)

        for row in range(ROWS):
            for col in range(COLS):
                tile = IsoTile(row, col)
                x, y = iso_to_screen(row, col)
                tile.setPos(x, y)
                self.scene.addItem(tile)
                self.scene.tiles[(row, col)] = tile

                text = None
                if isBorder(row, col) and (row == 0 or row == ROWS - 1) and not isCorner(row, col):
                    text = QGraphicsTextItem(cols[col])
                elif isBorder(row, col) and (col == 0 or col == COLS - 1) and not isCorner(row, col):
                    text = QGraphicsTextItem(rows[row])

                if text:
                    text.setPos(x + 20, y)
                    text.setDefaultTextColor(QColor("#0f1319"))
                    text.setFont(font)
                    text.setZValue(51)

                    shadow = QGraphicsDropShadowEffect()
                    shadow.setBlurRadius(2)
                    shadow.setXOffset(1)
                    shadow.setYOffset(1.5)
                    shadow.setColor(QColor("white"))
                    text.setGraphicsEffect(shadow)

                    self.scene.addItem(text)

                if is_pile(row, col):
                    add_image(self.scene, row, col, "pile.png")
                if row == 7 and col == 9:
                    add_image(self.scene, row, col, "cube.png")

        self.scene.setSceneRect(self.scene.itemsBoundingRect())

    # Molette : si le curseur est sur un totem Nox, change son niveau. Sinon, si le
    # curseur est dans le vide (hors de toute case, ex: les coins hors du losange),
    # cycle le token ou la couleur selectionne selon le mode actif. Sinon, zoom normal.
    def wheelEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        items_here = self.scene.items(scene_pos)

        nox = next((it for it in items_here if isinstance(it, NoxToken)), None)
        if nox:
            direction = 1 if event.angleDelta().y() > 0 else -1
            nox.change_level(direction)
            event.accept()
            return

        has_tile = any(isinstance(it, IsoTile) for it in items_here)
        if not has_tile:
            direction = 1 if event.angleDelta().y() > 0 else -1
            mw = self.main_window
            if mw and self.scene.mode == EditorMode.TOKEN:
                mw.cycle_token_type(direction)
                event.accept()
                return
            if mw and self.scene.mode == EditorMode.PAINT:
                mw.cycle_paint_color(direction)
                event.accept()
                return
            event.accept()
            return

        zoom_factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(zoom_factor, zoom_factor)
        else:
            self.scale(1 / zoom_factor, 1 / zoom_factor)
#onglets
class MapTab(QWidget):
    """Un onglet = une carte independante : sa propre vue/scene (creees par
    IsoView), son panneau de joueurs, et son chemin de fichier courant pour
    Sauvegarder. main_window et scene.main_window sont branches par
    MainWindow.add_new_tab juste apres la construction."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_file_path = None

        self.view = IsoView()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        self.player_panel = PlayerPanel(self.view.scene, parent=self.view)
        self.player_panel.refresh()
        self.reposition_player_panel()
        self.network_role = None
        self.network = None
        self.network_bridge = None

    def reposition_player_panel(self):
        margin = 10
        x = self.view.width() - self.player_panel.width() - margin
        self.player_panel.move(x, margin)
        self.player_panel.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reposition_player_panel()
#gere les onglets, la toolbar et le reseau
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nox Map Editor")
        self.resize(1100, 750)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.tabBarDoubleClicked.connect(self.rename_tab)
        self.tabs.tabBarClicked.connect(self.on_tab_bar_clicked)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.setCentralWidget(self.tabs)
        self.tabs.setStyleSheet(
            """
            QTabWidget::pane {
                border: none;
                background-color: #0f1319;
                top: -1px;
            }
            QTabWidget::tab-bar {
                left: 0px;
                background-color: #14171c;
            }
            QTabWidget {
                background-color: #14171c;
            }
            QTabBar {
                background-color: #14171c;
            }
            QTabBar::tab {
                background-color: #1f232a;
                color: #9aa0a6;
                border: 1px solid #2c313a;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 6px 16px;
                margin-right: 2px;
                font-size: 12px;
            }
            QTabBar::tab:hover {
                background-color: #272c34;
                color: #e8e8e8;
            }
            QTabBar::tab:selected {
                background-color: #4c5fd5;
                color: white;
                border-color: #6c7fee;
            }
            QTabBar::close-button {
                image: none;
                subcontrol-position: right;
            }
            QTabBar::close-button:hover {
                background-color: rgba(255,255,255,40);
                border-radius: 3px;
            }
            """
        )

        # L'onglet "+" est un vrai onglet (peint par QTabBar, meme style, aucune
        # zone non couverte possible), pas un corner widget. Il est cree une seule
        # fois et reste toujours en derniere position (voir add_new_tab/close_tab).
        self.tab_counter = 0
        self._add_plus_tab()
        self.add_new_tab()  # au moins une carte AVANT la toolbar
        self.build_toolbar()  # la toolbar lit l'etat de l'onglet actif
    def host_session(self):
        tab = self.current_tab()
        password = generate_password()
        bridge = NetworkBridge()
        server = HostServer(tab.view.scene, password, bridge)
        server.start(port=5555)

        tab.network_role = "host"
        tab.network = server
        tab.view.scene.on_local_op = server.send_op

        bridge.op_received.connect(tab.view.scene.apply_remote_op)
        tab.network_bridge = bridge  # garder une reference, sinon le GC peut la detruire

        ip = get_local_ip()
        QMessageBox.information(self, "Session hébergée",
                                f"IP : {ip}\nPort : 5555\nMot de passe : {password}")
    def connect_session(self):
        ip, ok1 = QInputDialog.getText(self, "Connexion", "IP de l'hôte :")
        if not ok1 or not ip.strip():
            return
        password, ok2 = QInputDialog.getText(self, "Connexion", "Mot de passe :")
        if not ok2:
            return

        tab = self.add_new_tab()
        bridge = NetworkBridge()
        client = ClientConnection(bridge)

        def on_snapshot(snapshot):
            tab.view.scene.clear_all()
            tab.view.draw_map()
            tab.view.scene.load_dict(snapshot)
            tab.view.scene.on_local_op = client.send_op

        bridge.snapshot_received.connect(on_snapshot)
        bridge.op_received.connect(tab.view.scene.apply_remote_op)
        bridge.auth_failed.connect(lambda: QMessageBox.warning(self, "Connexion refusée", "Mot de passe incorrect."))

        tab.network_role = "client"
        tab.network = client
        tab.network_bridge = bridge

        try:
            client.connect(ip.strip(), 5555, password.strip())
        except OSError as exc:
            QMessageBox.warning(self, "Connexion impossible", str(exc))
    def sync_toolbar_to_scene(self, scene):
        action = self.mode_actions.get(scene.mode)
        if action:
            action.setChecked(True)

        self.current_color_swatch.set_color(scene.current_color)
        self.rebuild_palette()

        self._sync_token_combo_display(scene.current_token_key)

        btn = self.nox_element_buttons.get(scene.nox_element)
        if btn:
            btn.setChecked(True)
    # Met a jour category_combo/token_combo pour REFLETER token_key, sans jamais
    # toucher au mode ni au current_token_key de la scene (contrairement a
    # on_category_changed/set_token_type, qui sont les slots declenches par
    # l'utilisateur et qui eux doivent basculer en mode Token)
    def _sync_token_combo_display(self, token_key):
        data = TOKEN_LIBRARY.get(token_key, {})
        category = data.get("category")

        cat_idx = self.category_combo.findData(category)
        if cat_idx != -1 and cat_idx != self.category_combo.currentIndex():
            self.category_combo.blockSignals(True)
            self.category_combo.setCurrentIndex(cat_idx)
            self.category_combo.blockSignals(False)
            self._populate_token_combo(category)

        idx = self.token_combo.findData(token_key)
        if idx != -1:
            self.token_combo.blockSignals(True)
            self.token_combo.setCurrentIndex(idx)
            self.token_combo.blockSignals(False)
    # Repeuple token_combo pour une categorie sans rien selectionner ni toucher
    # au mode (utilise par on_category_changed ET par _sync_token_combo_display)
    def _populate_token_combo(self, category):
        self.token_combo.blockSignals(True)
        self.token_combo.clear()
        for key, data in TOKEN_LIBRARY.items():
            if data.get("auto_only"):
                continue
            if data.get("category") == category:
                self.token_combo.addItem(build_token_icon(key), data["label"], key)
        self.token_combo.blockSignals(False)
    # Cree l'onglet "+" une seule fois, comme dernier onglet non fermable. Comme
    # c'est un vrai onglet peint par QTabBar, il herite du style existant sans
    # aucune zone non couverte (contrairement a l'ancien corner widget).
    def _add_plus_tab(self):
        placeholder = QWidget()
        self.tabs.addTab(placeholder, "+")
        self.plus_tab_index = self.tabs.count() - 1
        self.tabs.tabBar().setTabButton(self.plus_tab_index, QTabBar.RightSide, None)
    # Clic sur un onglet quelconque de la tab bar : si c'est l'onglet "+", cree
    # une nouvelle carte a la place (au lieu de "switcher" dessus, qui est vide)
    def on_tab_bar_clicked(self, index):
        if index == self.plus_tab_index:
            self.add_new_tab()
    def add_new_tab(self):
        self.tab_counter += 1
        tab = MapTab()
        tab.view.scene.main_window = self
        tab.view.main_window = self
        insert_at = self.plus_tab_index
        self.tabs.insertTab(insert_at, tab, f"{self.tab_counter}")
        self.plus_tab_index += 1
        self.tabs.setCurrentIndex(insert_at)
        return tab
    def close_tab(self, index):
        if index == self.plus_tab_index:
            return  # on ne peut pas fermer le bouton "+"
        if self.tabs.count() - 1 <= 1:
            return  # toujours garder au moins une carte ouverte (le "+" ne compte pas)
        widget = self.tabs.widget(index)
        self.tabs.removeTab(index)
        widget.deleteLater()
        if index < self.plus_tab_index:
            self.plus_tab_index -= 1
    def rename_tab(self, index):
        if index == self.plus_tab_index:
            return
        current_name = self.tabs.tabText(index)
        name, ok = QInputDialog.getText(self, "Renommer l'onglet", "Nom :", text=current_name)
        if ok and name.strip():
            self.tabs.setTabText(index, name.strip())
    def current_tab(self):
        return self.tabs.currentWidget()
    def on_tab_changed(self, index):
        if not hasattr(self, "mode_actions"):
            return  # toolbar pas encore construite (premier onglet)
        tab = self.current_tab()
        if isinstance(tab, MapTab):
            self.sync_toolbar_to_scene(tab.view.scene)
    def refresh_player_panel(self):
        tab = self.current_tab()
        if isinstance(tab, MapTab):
            tab.player_panel.refresh()
    def build_toolbar(self):
        toolbar = QToolBar("Outils")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setStyleSheet(
            """
            QToolBar {
                background-color: #14171c;
                border: none;
                padding: 6px;
                spacing: 6px;
            }
            QToolBar QLabel {
                color: #9aa0a6;
                font-size: 11px;
                font-weight: 600;
                padding: 0 2px;
            }
            QToolBar::separator {
                background-color: #2c313a;
                width: 1px;
                margin: 4px 8px;
            }
            QToolButton {
                background-color: #1f232a;
                color: #e8e8e8;
                border: 1px solid #2c313a;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
            }
            QToolButton:hover {
                background-color: #272c34;
                border-color: #3d434f;
            }
            QToolButton:checked {
                background-color: #4c5fd5;
                border-color: #6c7fee;
                color: white;
            }
            QComboBox {
                background-color: #1f232a;
                color: #e8e8e8;
                border: 1px solid #2c313a;
                border-radius: 6px;
                padding: 4px 10px;
                min-width: 130px;
            }
            QComboBox:hover {
                border-color: #3d434f;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background-color: #1f232a;
                color: #e8e8e8;
                selection-background-color: #4c5fd5;
                border: 1px solid #2c313a;
                outline: none;
                padding: 2px;
            }
            QPushButton#elementBtn {
                background-color: #1f232a;
                color: #e8e8e8;
                border: 1px solid #2c313a;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton#elementBtn:hover {
                background-color: #272c34;
                border-color: #3d434f;
            }
            QPushButton#elementBtn:checked {
                background-color: #4c5fd5;
                border-color: #6c7fee;
                color: white;
            }
            """
        )
        self.addToolBar(toolbar)

        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        self.mode_actions = {}

        def make_mode_action(label, mode):
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(lambda: self.current_tab().view.scene.set_mode(mode))
            mode_group.addAction(action)
            toolbar.addAction(action)
            self.mode_actions[mode] = action
            return action

        paint_action = make_mode_action("Peindre", EditorMode.PAINT)
        make_mode_action("Token", EditorMode.TOKEN)
        make_mode_action("Fleche", EditorMode.ARROW)
        make_mode_action("Effacer", EditorMode.ERASE)
        paint_action.setChecked(True)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Couleur : "))

        self.current_color_swatch = ColorSwatchButton(self.current_tab().view.scene.current_color)
        self.current_color_swatch.clicked.connect(self.pick_tile_color)
        toolbar.addWidget(self.current_color_swatch)

        toolbar.addWidget(QLabel(" : "))
        self.palette_container = QWidget()
        palette_layout = QHBoxLayout(self.palette_container)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.setSpacing(4)
        toolbar.addWidget(self.palette_container)
        self.rebuild_palette()

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Token : "))

        self.category_combo = QComboBox()
        self.category_combo.addItem("Mécanisme / Invocation", "mechanism")
        self.category_combo.addItem("Personnage joueur", "player")
        self.category_combo.addItem("Nox", "nox")
        self.category_combo.currentIndexChanged.connect(self.on_category_changed)
        toolbar.addWidget(self.category_combo)

        self.token_combo = QComboBox()
        self.token_combo.setIconSize(QSize(24, 24))
        self.token_combo.currentIndexChanged.connect(self.set_token_type)
        toolbar.addWidget(self.token_combo)
        self.on_category_changed(0)  # peuple la combo avec la premiere categorie

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Phase : "))

        self.nox_element_group = QButtonGroup(self)
        self.nox_element_group.setExclusive(True)
        self.nox_element_buttons = {}
        for key, label in [("eau", "Eau"), ("air", "Air"), ("feu", "Feu"), ("terre", "Terre")]:
            btn = QPushButton(label)
            btn.setObjectName("elementBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, k=key: self.set_nox_element(k))
            self.nox_element_group.addButton(btn)
            toolbar.addWidget(btn)
            self.nox_element_buttons[key] = btn
            if key == self.current_tab().view.scene.nox_element:
                btn.setChecked(True)

        toolbar.addSeparator()

        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence.Undo)
        undo_action.triggered.connect(lambda: self.current_tab().view.scene.undo())
        toolbar.addAction(undo_action)
        self.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence.Redo)
        redo_action.triggered.connect(lambda: self.current_tab().view.scene.redo())
        toolbar.addAction(redo_action)
        self.addAction(redo_action)

        toolbar.addSeparator()

        import_action = QAction("Import", self)
        import_action.triggered.connect(self.import_map)
        toolbar.addAction(import_action)

        export_action = QAction("Export", self)
        export_action.triggered.connect(self.export_map)
        toolbar.addAction(export_action)

        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence.Save)  # Ctrl+S
        save_action.triggered.connect(self.save_map)
        toolbar.addAction(save_action)
        self.addAction(save_action)
        toolbar.addSeparator()

        host_action = QAction("Host", self)
        host_action.triggered.connect(self.host_session)
        toolbar.addAction(host_action)

        connect_action = QAction("Connect", self)
        connect_action.triggered.connect(self.connect_session)
        toolbar.addAction(connect_action)
        toolbar.addSeparator()

        clear_action = QAction("Tout effacer", self)
        clear_action.triggered.connect(self.clear_scene)
        toolbar.addAction(clear_action)
    def pick_tile_color(self):
        color = QColorDialog.getColor(self.current_tab().view.scene.current_color, self, "Choisir une couleur de case")
        if color.isValid():
            self.apply_paint_color(color)
    def apply_paint_color(self, color):
        scene = self.current_tab().view.scene
        scene.current_color = color
        scene.add_recent_color(color)
        scene.set_mode(EditorMode.PAINT)
        self.current_color_swatch.set_color(color)
        self.rebuild_palette()
    def rebuild_palette(self):
        layout = self.palette_container.layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        for color in self.current_tab().view.scene.recent_colors:
            swatch = ColorSwatchButton(color)
            swatch.clicked.connect(lambda checked=False, c=QColor(color): self.apply_paint_color(c))
            layout.addWidget(swatch)
    # Repeuple la combo de type de token avec uniquement les entrees de la categorie
    # choisie (mecanisme/invocation, personnage joueur, ou nox)
    def on_category_changed(self, index):
        category = self.category_combo.itemData(index)
        self._populate_token_combo(category)
        if self.token_combo.count() > 0:
            self.token_combo.setCurrentIndex(0)
            self.set_token_type(0)
    def set_token_type(self, index):
        scene = self.current_tab().view.scene
        scene.current_token_key = self.token_combo.itemData(index)
        scene.set_mode(EditorMode.TOKEN)
    # Change l'element utilise pour le hover des totems Nox, et rafraichit le
    # surlignage immediatement si un Nox est en train d'etre survole
    def set_nox_element(self, key):
        scene = self.current_tab().view.scene
        scene.nox_element = key
        if scene.hovering_nox:
            scene.highlight_nox(scene.hovering_nox)
    # Molette dans le vide en mode Token : passe au type suivant/precedent dans la combo
    def cycle_token_type(self, direction):
        count = self.token_combo.count()
        if count == 0:
            return
        idx = (self.token_combo.currentIndex() + direction) % count
        self.token_combo.setCurrentIndex(idx)
    # Molette dans le vide en mode Peindre : passe a la couleur precedente/suivante
    # dans l'historique recent (sans le reordonner, contrairement a un clic sur la palette)
    def cycle_paint_color(self, direction):
        scene = self.current_tab().view.scene
        colors = scene.recent_colors
        if not colors:
            return
        names = [c.name() for c in colors]
        current_name = scene.current_color.name()
        idx = names.index(current_name) if current_name in names else 0
        idx = (idx + direction) % len(colors)
        scene.current_color = QColor(colors[idx])
        self.current_color_swatch.set_color(scene.current_color)
    def clear_scene(self):
        tab = self.current_tab()
        tab.view.scene.clear_all()
        tab.view.draw_map()
        tab.view.scene._emit_op({"type": "clear_all"})
    # Coche le bon bouton d'element Nox apres un import (qui change scene.nox_element
    # directement, sans passer par set_nox_element)
    def sync_nox_element_button(self):
        btn = self.nox_element_buttons.get(self.current_tab().view.scene.nox_element)
        if btn:
            btn.setChecked(True)
    # Charge une carte depuis un fichier JSON choisi par l'utilisateur : vide la
    # carte actuelle, la reconstruit vierge, puis y applique l'etat charge. Le
    # fichier choisi devient le fichier courant (utilise ensuite par Sauvegarder).
    def import_map(self):
        path, _ = QFileDialog.getOpenFileName(self, "Importer une carte", "", "Carte (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Import impossible", f"Impossible de lire ce fichier :\n{exc}")
            return

        self.current_tab().view.scene.clear_all()
        self.current_tab().view.draw_map()
        self.current_tab().view.scene.load_dict(data)
        self.current_tab().current_file_path = path
    # Exporte la carte actuelle vers un fichier JSON choisi par l'utilisateur (avec
    # dialogue "Enregistrer sous"). Le fichier choisi devient le fichier courant.
    def export_map(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exporter la carte", "", "Carte (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        if self._write_map(path):
            self.current_tab().current_file_path = path
    # Ctrl+S / bouton Sauvegarder : ecrit directement sur le fichier courant s'il y
    # en a deja un (pas de dialogue). S'il n'y a pas encore de fichier courant (jamais
    # importe/exporte), se comporte comme Exporter (demande ou sauvegarder).
    def save_map(self):
        tab = self.current_tab()
        if tab.current_file_path:
            self._write_map(tab.current_file_path)
        else:
            self.export_map()
    # Ecrit l'etat actuel de la carte au format JSON vers `path`. Retourne True en
    # cas de succes, False sinon (et affiche un message d'erreur).
    def _write_map(self, path):
        data = self.current_tab().view.scene.to_dict()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except OSError as exc:
            QMessageBox.warning(self, "Sauvegarde impossible", f"Impossible d'ecrire ce fichier :\n{exc}")
            return False
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())