import sys
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPolygonItem,
    QGraphicsPixmapItem,
    QGraphicsTextItem
)
from PyQt5.QtGui import QPolygonF, QBrush, QColor, QPen, QPainter, QPixmap, QFont
from PyQt5.QtCore import QPointF, Qt


TILE_WIDTH = 64
TILE_HEIGHT = 32
ROWS = 16
COLS = 18

def isBorder(row, col):
    return (row == 0 or col == 0 or row == ROWS - 1 or col == COLS - 1)
def isCorner(row, col):
    return (row in [0, ROWS - 1] and col in [0, COLS - 1])
def iso_to_screen(row, col):
    x = (col - row) * (TILE_WIDTH // 2)
    y = (col + row) * (TILE_HEIGHT // 2)
    return x, y
def add_image(scene, row, col, image_path):
    multiplier = 1
    if image_path == "cube.png":
        multiplier = 2
    pixmap = QPixmap(image_path).scaled(
    int(96 * multiplier),
    int(96 *multiplier),
    Qt.KeepAspectRatio,
    Qt.SmoothTransformation
)
    item = QGraphicsPixmapItem(pixmap)

    x, y = iso_to_screen(row, col)

    item.setPos(
        x + TILE_WIDTH/2 - pixmap.width()/2,
        y + TILE_HEIGHT/2 - pixmap.height() - 2.5
    )

    item.setZValue(row + col + 10)

    scene.addItem(item)
def is_pile(col, row):
    return ((col == 4 and row == 4) or
            (col == 3 and row == 12) or
            (col == 14 and row == 10) or
            (col == 9 and row == 16) or
            (col == 11 and row == 3))

class IsoTile(QGraphicsPolygonItem):
    def __init__(self, row, col):
        super().__init__()
        self.row = row
        self.col = col
        self.selected = False
        colorborder = QColor("lightblue")
        w = TILE_WIDTH
        h = TILE_HEIGHT

        polygon = QPolygonF([
            QPointF(0, h / 2),
            QPointF(w / 2, 0),
            QPointF(w, h / 2),
            QPointF(w / 2, h),
        ])

        self.setPolygon(polygon)
        self.setPen(QPen(QColor("black")))
        if ((row + col) % 2):
            self.default_brush = QBrush(QColor("darkgrey"))
        else:
            self.default_brush = QBrush(QColor("lightgrey"))

        if isBorder(row, col):
            if (isCorner(row, col)):
                self.default_brush = (QBrush(QColor("black")))
            else:
                self.default_brush = (QBrush(colorborder))

        self.hover_brush = QBrush(QColor(100, 120, 160))
        self.selected_brush = QBrush(QColor(180, 80, 80))
        self.setBrush(self.default_brush)
        self.setAcceptHoverEvents(True)
        # depth sorting
        self.setZValue(row + col)

    def hoverEnterEvent(self, event):
        if not self.selected:
            self.setBrush(self.hover_brush)

    def hoverLeaveEvent(self, event):
        if not self.selected:
            self.setBrush(self.default_brush)

    def mousePressEvent(self, event):
        self.selected = not self.selected
        if self.selected:
            self.setBrush(self.selected_brush)
        else:
            self.setBrush(self.default_brush)

        print(f"Clicked tile {self.row}, {self.col}")


class IsoView(QGraphicsView):
    def __init__(self):
        super().__init__()

        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)

        self.draw_map()

    def draw_map(self):
        cols = ["A", "B", "C", "D", "E", "F","G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S"]
        rows = ["0","1", "2", "3","4","5", "6", "7","8","9", "10", "11","12","13", "14", "15", "", ""]
        font = QFont("Consolas", 11, QFont.Bold)

        for row in range(ROWS):
            hasText = False
            for col in range(COLS):
                tile = IsoTile(row, col)
                x, y = iso_to_screen(row, col)
                tile.setPos(x, y)
                if (isBorder(row, col) and row == 0):
                    print("row coordonate: ", cols[col])
                    text = QGraphicsTextItem(rows[row])
                    hasText = True
                elif(isBorder(row, col) and col == 0):
                    hasText = True
                    print("col coordonate: ", rows[row])
                    text = QGraphicsTextItem(cols[col])
                if hasText:
                    x,y = iso_to_screen(row, col)
                    text.setPos(x + 20, y - 10)
                    text.setDefaultTextColor(QColor("crimson"))
                    text.setFont(font)
                    self.scene.addItem(text)
                self.scene.addItem(tile)

                # add pile AFTER tile is in scene
                if is_pile(row, col):
                    add_image(self.scene, row, col, "pile.png")
                if (row == 7 and col == 9):
                    add_image(self.scene, row, col, "cube.png")

        self.scene.setSceneRect(self.scene.itemsBoundingRect())

    def wheelEvent(self, event):
        zoom_factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(zoom_factor, zoom_factor)
        else:
            self.scale(1 / zoom_factor, 1 / zoom_factor)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    view = IsoView()
    view.setWindowTitle("Isometric Wakfu-Style Map")
    view.resize(900, 700)
    view.show()
    sys.exit(app.exec_())