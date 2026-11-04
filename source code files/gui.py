
import ctypes
import datetime
import os
import shutil
import subprocess
import sys
import time
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_double,
    c_int,
    c_int16,
    c_int32,
    c_int64,
    c_uint8,
    c_uint32,
    create_string_buffer,
)

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QRectF,
    QStorageInfo,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Disk Visualizer"
DLL_NAME = "logic.dll" if sys.platform == "win32" else "logic.so"
DB_NAME = "DiskVisualizer.db"
ROOT_LABEL = "This PC"
TITLEBAR_HEIGHT = 32
RESIZE_MARGIN = 5

C_TITLEBAR = "#3C3C3C"
C_BODY = "#1E1E1E"
C_PANEL = "#252526"
C_TEXT = "#CCCCCC"
C_MUTED = "#8A8A8A"
C_HOVER = "#505050"
C_ACCENT = "#007ACC"
C_CLOSE_HOVER = "#E81123"
C_ERR = "#F48771"

# File categories — index == the DV_CAT_* enum in logic.c (checked at start-up).
CATEGORIES = [
    ("Folders", "#3E4A5E"),
    ("Images", "#E8B339"),
    ("Video", "#E5534B"),
    ("Audio", "#B083F0"),
    ("Documents", "#4C9AFF"),
    ("Archives", "#DB7A2C"),
    ("Executables", "#57AB5A"),
    ("Code", "#39C5CF"),
    ("Data & system", "#B7A57A"),
    ("Other", "#868B94"),
]
CAT_FOLDER = 0
ALL_CATS = (1 << len(CATEGORIES)) - 1

# Mirrors of logic.c constants.
DV_OK, DV_ERR_BAD_INDEX, DV_ERR_BAD_BUF, DV_ERR_NOMEM, DV_ERR_OPEN_ROOT = 0, 1, 2, 3, 4
DV_ERR_CANCELLED, DV_ERR_BUSY, DV_ERR_DB_OPEN, DV_ERR_DB_WRITE = 5, 6, 7, 8
DV_ERR_DB_CORRUPT, DV_ERR_NO_DATA, DV_ERR_BAD_ARG = 9, 10, 11
DV_ERR_NO_CHANGE = 12
PHASE_SCAN, PHASE_INDEX, PHASE_SAVE, PHASE_LOAD, PHASE_VERIFY = 0, 1, 2, 3, 4
FLAG_DENIED, FLAG_LINK, FLAG_MOUNT, FLAG_VIRTUAL = 0x01, 0x02, 0x04, 0x08
FLAG_PROBLEM = FLAG_DENIED | FLAG_LINK | FLAG_MOUNT     # rows drawn greyed out

# What a node is, for icons / labels.
KIND_ITEM, KIND_PC, KIND_DRIVE = 0, 1, 2


# =========================================================================== ctypes layer
#
# The C side hands out three kinds of data:
#   * integers (node indices are plain int32 handles into the C node array),
#   * UTF-8 strings written into buffers we allocate with create_string_buffer(),
#   * fixed-layout structs. Each Structure below repeats the C field list in the
#     same order with the same widths, so ctypes computes the same padding as the
#     C compiler. dv_abi_check() compares sizeof() on both sides at start-up.

class DvInfo(Structure):
    """struct dv_info — one file or folder."""
    _fields_ = [
        ("size", c_int64),         # bytes (folders: recursive total)
        ("mtime", c_int64),        # Unix seconds
        ("atime", c_int64),        # last opened, Unix seconds
        ("parent", c_int32),       # -1 for the root
        ("child_count", c_int32),
        ("rank", c_int32),         # row among its siblings, largest first
        ("file_count", c_int32),   # folders: files anywhere below
        ("is_dir", c_uint8),
        ("category", c_uint8),
        ("flags", c_uint8),
        ("reserved", c_uint8),
    ]


class DvRect(Structure):
    """struct dv_rect — one treemap rectangle computed by the C layout."""
    _fields_ = [
        ("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double),
        ("size", c_int64),
        ("node", c_int32),
        ("depth", c_int16),
        ("is_dir", c_uint8),
        ("category", c_uint8),
        ("expanded", c_uint8),
        ("reserved", c_uint8 * 7),
    ]


class DvStats(Structure):
    """struct dv_stats — totals for the data currently in RAM."""
    _fields_ = [
        ("files", c_int64), ("dirs", c_int64), ("bytes", c_int64), ("skipped", c_int64),
        ("scan_ms", c_int64), ("scanned_at", c_int64),
        ("node_count", c_int32), ("has_data", c_int32),
    ]


# void (*)(int phase, int64 files, int64 dirs, int64 bytes, int64 done, int64 total, const char *path)
# ctypes turns the const char* into a Python bytes object for us.
PROGRESS_FN = ctypes.CFUNCTYPE(None, c_int, c_int64, c_int64, c_int64, c_int64, c_int64, c_char_p)

# name: (restype, argtypes) — declaring these stops ctypes from guessing
# (it would otherwise truncate int64 values and pointers to C int).
_PROTOTYPES = {
    "dv_version": (c_int, []),
    "dv_abi_check": (c_int, [c_int, c_int, c_int]),
    "dv_category_count": (c_int, []),
    "dv_scan": (c_int, [c_char_p, c_char_p, c_char_p, PROGRESS_FN]),
    "dv_refresh": (c_int, [c_char_p, c_char_p, c_char_p, PROGRESS_FN]),
    "dv_load_db": (c_int, [c_char_p, PROGRESS_FN]),
    "dv_save_db": (c_int, [c_char_p, PROGRESS_FN]),
    "dv_cancel": (None, []),
    "dv_is_busy": (c_int, []),
    "dv_get_stats": (c_int, [POINTER(DvStats)]),
    "dv_node_info": (c_int, [c_int32, POINTER(DvInfo)]),
    "dv_node_name": (c_int, [c_int32, c_char_p, c_int]),
    "dv_node_path": (c_int, [c_int32, c_char_p, c_int]),
    "dv_children": (c_int, [c_int32, c_int32, POINTER(c_int32), c_int32]),
    "dv_find_path": (c_int32, [c_char_p]),
    "dv_search": (c_int, [c_char_p, c_uint32, c_int64, c_int, POINTER(c_int32), c_int, POINTER(c_int64)]),
    "dv_treemap": (c_int, [c_int32, c_double, c_double, c_double, c_double, c_int,
                           c_double, c_double, c_double, POINTER(DvRect), c_int]),
    "dv_category_totals": (c_int, [POINTER(c_int64), POINTER(c_int64), c_int]),
    "dv_error_text": (c_int, [c_int, c_char_p, c_int]),
    "dv_last_error": (c_int, [c_char_p, c_int]),
    "dv_find_old": (c_int, [c_int64, c_int64, POINTER(c_int32), c_int, POINTER(c_int64), POINTER(c_int64)]),
    "dv_find_named_dirs": (c_int, [c_char_p, POINTER(c_int32), c_int, POINTER(c_int64), POINTER(c_int64)]),
}
DV_MIN_VERSION = 0x00030000     # incremental dv_refresh() + dv_find_path()


def _base_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _u8(b) -> str:
    return b.decode("utf-8", "replace") if b else ""


# ---------------------------------------------------------------- DLL dependency help
# MinGW-built DLLs commonly depend on these when not linked with -static.
_MINGW_RUNTIME = ("libsqlite3-0.dll", "libgcc_s_seh-1.dll", "libgcc_s_dw2-1.dll",
                  "libwinpthread-1.dll", "libstdc++-6.dll")
_PE_MACHINES = {0x014C: "32-bit (x86)", 0x8664: "64-bit (x64)", 0xAA64: "ARM64"}


def _msys2_bin_dirs():
    """Likely MSYS2 / MinGW bin folders, most specific first."""
    roots = []
    for env in ("MSYS2_ROOT", "MSYS2_PATH"):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    for drive in ("C:", "D:"):
        roots += [drive + "\\msys64", drive + "\\msys2", drive + "\\tools\\msys64"]
    dirs = []
    for r in roots:
        for sub in ("mingw64", "ucrt64", "clang64", "mingw32"):
            dirs.append(os.path.join(r, sub, "bin"))
    dirs += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def _pe_info(path):
    """(machine, [imported DLL names]) read straight from a PE file's import table,
    or (None, []) if the file cannot be parsed. Pure Python — no extra packages."""
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return None, []
        machine, nsect = struct.unpack_from("<HH", data, pe + 4)
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        ddir = opt + (112 if magic == 0x20B else 96)
        imp_rva = struct.unpack_from("<I", data, ddir + 8)[0]   # data directory 1 = imports
        sections = []
        sec = opt + opt_size
        for k in range(nsect):
            vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, sec + k * 40 + 8)
            sections.append((va, max(vsize, rawsize), rawptr))

        def off(rva):
            for va, size, raw in sections:
                if va <= rva < va + size:
                    return rva - va + raw
            return None

        names = []
        d = off(imp_rva) if imp_rva else None
        while d is not None and d + 20 <= len(data):
            name_rva = struct.unpack_from("<I", data, d + 12)[0]
            if name_rva == 0:
                break
            o = off(name_rva)
            if o is not None:
                end = data.find(b"\0", o)
                names.append(data[o:end].decode("ascii", "replace"))
            d += 20
        return machine, names
    except (OSError, struct.error, ValueError):
        return None, []


def _register_dll_dirs(base, dll_path):
    """Add the folder of logic.dll plus any folder that holds a MinGW runtime DLL
    that logic.dll imports but is not sitting next to it. Returns the handles
    (they must stay referenced, or the directories are removed again)."""
    handles = []
    if sys.platform != "win32":
        return handles
    handles.append(os.add_dll_directory(base))
    _m, imports = _pe_info(dll_path)
    wanted = {n.lower() for n in imports} | {"libsqlite3-0.dll"}
    missing = {n for n in _MINGW_RUNTIME
               if n in wanted and not os.path.exists(os.path.join(base, n))}
    if not missing:
        return handles
    for d in _msys2_bin_dirs():
        if any(os.path.exists(os.path.join(d, n)) for n in missing):
            try:
                handles.append(os.add_dll_directory(d))
            except OSError:
                continue
            missing = {n for n in missing if not os.path.exists(os.path.join(d, n))}
            if not missing:
                break
    return handles


def _find_missing_deps(dll_path, base):
    """Walk the import tables (recursively, for non-system DLLs) and return the
    names of DLLs Windows will not be able to find."""
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    search = [base, sysdir, os.path.dirname(sys.executable)] + _msys2_bin_dirs()
    missing, seen, todo = [], set(), [dll_path]
    while todo:
        cur = todo.pop()
        _m, imports = _pe_info(cur)
        for name in imports:
            low = name.lower()
            if low in seen or low.startswith(("api-ms-", "ext-ms-")):
                continue
            seen.add(low)
            hit = next((os.path.join(d, name) for d in search
                        if os.path.exists(os.path.join(d, name))), None)
            if hit is None:
                missing.append(name)
            elif not os.path.normcase(hit).startswith(os.path.normcase(sysdir)):
                todo.append(hit)
    return missing


def _explain_load_failure(dll_path, base, err) -> str:
    head = f"{DLL_NAME} could not be loaded:\n{err}"
    if sys.platform != "win32":
        return head
    import struct
    machine, _imports = _pe_info(dll_path)
    py_bits = struct.calcsize("P") * 8
    want = 0x8664 if py_bits == 64 else 0x014C
    if machine is not None and machine != want:
        return (f"{head}\n\n{DLL_NAME} is {_PE_MACHINES.get(machine, hex(machine))} but this "
                f"Python is {py_bits}-bit. Rebuild it in the MSYS2 "
                f"{'MINGW64' if py_bits == 64 else 'MINGW32'} shell.")
    missing = _find_missing_deps(dll_path, base)
    if missing:
        lst = "\n".join(f"  • {m}" for m in missing)
        return (f"{head}\n\nMissing DLL(s) that {DLL_NAME} needs:\n{lst}\n\n"
                f"Fix (pick one):\n"
                f"  1. Rebuild a self-contained {DLL_NAME} in the MSYS2 MINGW64 shell:\n"
                f"       gcc -shared -O2 -Wall logic.c -o logic.dll -static -lsqlite3\n"
                f"  2. Or copy the files above from C:\\msys64\\mingw64\\bin into\n"
                f"       {base}")
    return (f"{head}\n\nRebuild a self-contained {DLL_NAME} in the MSYS2 MINGW64 shell:\n"
            f"  gcc -shared -O2 -Wall logic.c -o logic.dll -static -lsqlite3")


# ---------------------------------------------------------------- drives
_PSEUDO_FS = {"tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "squashfs", "overlay",
              "autofs", "debugfs", "tracefs", "securityfs", "pstore", "efivarfs", "bpf",
              "configfs", "fusectl", "mqueue", "hugetlbfs", "ramfs", "nsfs", "binfmt_misc"}
_PSEUDO_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/var/lib/docker")
_WIN_DRIVE_TYPES = (2, 3, 6)      # DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_RAMDISK


def list_drives():
    """Every local disk / drive that is mounted and ready, as root paths
    ("C:\\", "D:\\" on Windows; "/", "/home", … elsewhere). Network shares,
    empty card readers / DVD drives and pseudo file systems are left out."""
    out, seen = [], set()
    for vol in QStorageInfo.mountedVolumes():
        if not (vol.isValid() and vol.isReady()):
            continue
        p = os.path.normpath(vol.rootPath())
        if not p:
            continue
        if sys.platform == "win32":
            if not p.endswith("\\"):
                p += "\\"
            try:
                kind = ctypes.windll.kernel32.GetDriveTypeW(p)
            except (AttributeError, OSError):
                kind = 3
            if kind not in _WIN_DRIVE_TYPES:
                continue
        else:
            if p != "/" and p.startswith(_PSEUDO_PREFIXES):
                continue
            fs = bytes(vol.fileSystemType()).decode("ascii", "replace").lower()
            if fs in _PSEUDO_FS:
                continue
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    out.sort(key=os.path.normcase)
    return out


_drive_labels = {}


def drive_label(path: str) -> str:
    """Friendly name for a drive root: "Local Disk (C:)", "Data (D:)", "/home"."""
    if path in _drive_labels:
        return _drive_labels[path]
    si = QStorageInfo(path)
    name = si.name().strip() if si.isValid() else ""
    if sys.platform == "win32":
        letter = path.rstrip("\\/")
        if len(letter) == 2 and letter[1] == ":":
            text = f"{name or 'Local Disk'} ({letter.upper()})"
        else:
            text = f"{name} ({path})" if name else path
    else:
        text = f"{name} ({path})" if name and name != path else path
    _drive_labels[path] = text
    return text


class DiskEngine:
    """Thin, typed wrapper around logic.dll. Query methods are cheap and may be
    called from the GUI thread; scan/load/save block and belong on the worker."""

    def __init__(self):
        base = _base_dir()
        path = os.path.join(base, DLL_NAME)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"{DLL_NAME} was not found next to gui.py.\n\n"
                f"Build it from logic.c first (see the build notes at the top of logic.c)."
            )
        # Python 3.8+ does NOT search PATH for a DLL's own dependencies, so a
        # logic.dll built in the MSYS2 shell (where mingw64\bin is on PATH) loads
        # there but fails from a normal double-click / VS Code run with
        # "Could not find module ... (or one of its dependencies)".
        # Register the folders that actually hold those runtime DLLs.
        self._dll_dirs = _register_dll_dirs(base, path)
        try:
            d = self._dll = ctypes.CDLL(path)
        except OSError as e:
            raise OSError(_explain_load_failure(path, base, e)) from e
        for name, (res, args) in _PROTOTYPES.items():
            try:
                fn = getattr(d, name)
            except AttributeError:
                raise RuntimeError(f"{DLL_NAME} is out of date (missing {name}). Rebuild it from logic.c.")
            fn.restype, fn.argtypes = res, args

        if d.dv_version() < DV_MIN_VERSION:
            raise RuntimeError(f"{DLL_NAME} is out of date (no multi-drive scan). Rebuild it from logic.c.")
        if d.dv_abi_check(ctypes.sizeof(DvInfo), ctypes.sizeof(DvRect), ctypes.sizeof(DvStats)) != DV_OK:
            raise RuntimeError(f"{DLL_NAME} does not match gui.py (struct layout). Rebuild it from logic.c.")
        if d.dv_category_count() != len(CATEGORIES):
            raise RuntimeError(f"{DLL_NAME} does not match gui.py (category list). Rebuild it from logic.c.")

        self._info = DvInfo()                       # reused output struct
        self._name_buf = create_string_buffer(2048)
        self._path_buf = create_string_buffer(4096)

    # ---- jobs (worker thread only) -------------------------------------------
    def scan(self, roots, label: str, db_path: str, cb) -> int:
        """roots: list of drive root paths, scanned under one virtual root."""
        return self._dll.dv_scan("\n".join(roots).encode("utf-8"), label.encode("utf-8"),
                                 db_path.encode("utf-8"), cb)

    def refresh(self, roots, label: str, db_path: str, cb) -> int:
        """Re-check the drives against the index already in RAM and rebuild only
        what changed. DV_ERR_NO_CHANGE means the index was left untouched."""
        return self._dll.dv_refresh("\n".join(roots).encode("utf-8"), label.encode("utf-8"),
                                    db_path.encode("utf-8"), cb)

    def load_db(self, db_path: str, cb) -> int:
        return self._dll.dv_load_db(db_path.encode("utf-8"), cb)

    def save_db(self, db_path: str, cb) -> int:
        return self._dll.dv_save_db(db_path.encode("utf-8"), cb)

    def cancel(self):
        self._dll.dv_cancel()           # just sets a flag — safe from any thread

    # ---- queries --------------------------------------------------------------
    def stats(self) -> DvStats:
        s = DvStats()
        self._dll.dv_get_stats(byref(s))
        return s

    def has_data(self) -> bool:
        return bool(self.stats().has_data)

    def info(self, idx: int):
        """Copy of struct dv_info for node idx (None if the index is stale)."""
        if self._dll.dv_node_info(idx, byref(self._info)) != DV_OK:
            return None
        return DvInfo.from_buffer_copy(self._info)  # value copy; the C output struct stays reusable

    def name(self, idx: int) -> str:
        if self._dll.dv_node_name(idx, self._name_buf, len(self._name_buf)) != DV_OK:
            return "?"
        return _u8(self._name_buf.value)

    def path(self, idx: int) -> str:
        buf = self._path_buf
        n = self._dll.dv_node_path(idx, buf, len(buf))
        if n < 0:
            return ""
        if n >= len(buf):                            # snprintf-style truncation → grow & retry
            buf = self._path_buf = create_string_buffer(n + 256)
            n = self._dll.dv_node_path(idx, buf, len(buf))
        return _u8(buf.value)

    def find_path(self, path: str) -> int:
        """Node index for a full path, or -1. A scan / refresh replaces the whole
        index, so this is how a position is carried across one."""
        if not path:
            return -1
        idx = self._dll.dv_find_path(path.encode("utf-8"))
        return idx if idx >= 0 else -1

    def children(self, idx: int, offset: int, count: int):
        arr = (c_int32 * max(count, 1))()           # C writes int32 indices into this array
        n = self._dll.dv_children(idx, offset, arr, count)
        return list(arr[:n]) if n > 0 else []

    def root_is_virtual(self) -> bool:
        info = self.info(0)
        return info is not None and bool(info.flags & FLAG_VIRTUAL)

    def kind(self, idx: int, info=None) -> int:
        """KIND_PC for "This PC", KIND_DRIVE for a disk / drive, else KIND_ITEM."""
        if info is None:
            info = self.info(idx)
        if info is None:
            return KIND_ITEM
        if info.flags & FLAG_VIRTUAL:
            return KIND_PC
        if info.parent == 0 and self.root_is_virtual():
            return KIND_DRIVE
        return KIND_ITEM

    def label(self, idx: int, info=None) -> str:
        """Name as shown to the user (drives get their volume label)."""
        name = self.name(idx)
        return drive_label(name) if self.kind(idx, info) == KIND_DRIVE else name

    def drives(self):
        """Node indices of the scanned drives (children of "This PC")."""
        if not self.has_data() or not self.root_is_virtual():
            return []
        info = self.info(0)
        return self.children(0, 0, info.child_count if info else 0)

    def search(self, query: str, cat_mask: int, min_size: int, kind: int, limit: int):
        arr = (c_int32 * max(limit, 1))()
        total = c_int64(0)
        n = self._dll.dv_search(query.encode("utf-8"), cat_mask, min_size, kind, arr, limit, byref(total))
        return (list(arr[:n]) if n > 0 else []), total.value

    def treemap(self, root, x, y, w, h, depth, min_side, header, pad, out_array) -> int:
        """Fills out_array (a DvRect * N ctypes array) and returns how many were written."""
        return self._dll.dv_treemap(root, x, y, w, h, depth, min_side, header, pad,
                                    out_array, len(out_array))

    def category_totals(self):
        n = len(CATEGORIES)
        b, c = (c_int64 * n)(), (c_int64 * n)()
        self._dll.dv_category_totals(b, c, n)
        return list(b), list(c)

    def error_text(self, code: int) -> str:
        buf = create_string_buffer(256)
        self._dll.dv_error_text(code, buf, len(buf))
        return _u8(buf.value) or f"Error {code}"

    def last_error(self) -> str:
        buf = create_string_buffer(1024)
        self._dll.dv_last_error(buf, len(buf))
        return _u8(buf.value)

    # ---- list pages ------------------------------------------------------------
    def find_old(self, cutoff: int, min_size: int, limit: int):
        """Files not changed or opened since `cutoff` → (ids biggest first, count, bytes)."""
        arr = (c_int32 * max(limit, 1))()
        total, nbytes = c_int64(0), c_int64(0)
        n = self._dll.dv_find_old(cutoff, min_size, arr, limit, byref(total), byref(nbytes))
        return (list(arr[:n]) if n > 0 else []), total.value, nbytes.value

    def find_named_dirs(self, names, limit: int):
        """Outermost folders named like one of `names` → (ids biggest first, count, bytes)."""
        arr = (c_int32 * max(limit, 1))()
        total, nbytes = c_int64(0), c_int64(0)
        n = self._dll.dv_find_named_dirs("|".join(names).encode("utf-8"), arr, limit,
                                         byref(total), byref(nbytes))
        return (list(arr[:n]) if n > 0 else []), total.value, nbytes.value


# =========================================================================== helpers
def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def type_text(name: str, is_dir: bool, cat: int, file_count: int = 0, kind: int = KIND_ITEM) -> str:
    if kind == KIND_PC:
        return f"All drives · {file_count:,} files"
    if kind == KIND_DRIVE:
        return f"Drive · {file_count:,} files"
    if is_dir:
        return f"Folder · {file_count:,} files"
    parts = name.split(".")[1:] if not name.startswith(".") else []
    while len(parts) > 1 and parts[-1].isdigit():     # libfoo.so.1.29 → SO (same rule as logic.c)
        parts.pop()
    ext = parts[-1].upper() if parts and 0 < len(parts[-1]) <= 15 else ""
    label = CATEGORIES[cat][0] if 0 <= cat < len(CATEGORIES) else "Other"
    return f"{ext} · {label}" if ext else label


def flag_text(flags: int) -> str:
    if flags & FLAG_DENIED:
        return "Access denied — contents not counted"
    if flags & FLAG_LINK:
        return "Link / junction — not followed"
    if flags & FLAG_MOUNT:
        return "Another file system — scanned as its own drive"
    return ""


def open_path(path: str):
    """Open a file / folder with the system's default program."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def show_in_file_manager(path: str, is_dir: bool = False):
    """Open the parent folder with the item selected (Windows) / the folder (others)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path if is_dir else os.path.dirname(path)))


def default_db_path() -> str:
    """Per-user data folder: always writable by a standard user."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "DiskVisualizer")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        folder = os.path.join(base, "disk-visualizer")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, DB_NAME)


def delete_db_files(path: str):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


_icon_cache = {}


def category_icon(cat: int, dim: bool = False) -> QIcon:
    key = (cat, dim)
    if key in _icon_cache:
        return _icon_cache[key]
    color = QColor(CATEGORIES[cat][1])
    if cat == CAT_FOLDER:
        color = QColor("#C9A04B")
    if dim:
        color = QColor("#6A6A6A")
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    if cat == CAT_FOLDER:
        p.drawRoundedRect(QRectF(1, 3, 6, 3), 1, 1)       # tab
        p.drawRoundedRect(QRectF(1, 5, 14, 9), 1.5, 1.5)  # body
    else:
        p.drawRoundedRect(QRectF(3, 2, 10, 12), 2, 2)
    p.end()
    icon = _icon_cache[key] = QIcon(pm)
    return icon


def kind_icon(kind: int, dim: bool = False) -> QIcon:
    """Hand-drawn icons for "This PC" (a monitor) and a drive (a disk box)."""
    key = ("kind", kind, dim)
    if key in _icon_cache:
        return _icon_cache[key]
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QColor("#6A6A6A" if dim else "#9DB4CC")
    if kind == KIND_PC:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(body)
        p.drawRoundedRect(QRectF(1, 2, 14, 9.5), 1.5, 1.5)          # screen frame
        p.setBrush(QColor("#1E3A55"))
        p.drawRect(QRectF(2.5, 3.5, 11, 6.5))                       # screen
        p.setBrush(body)
        p.drawRect(QRectF(7, 11.5, 2, 2))                           # neck
        p.drawRoundedRect(QRectF(4, 13.2, 8, 1.6), 0.8, 0.8)        # stand
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(body)
        p.drawRoundedRect(QRectF(1, 4, 14, 8), 2, 2)                # drive body
        p.setBrush(QColor("#2D3640"))
        p.drawRect(QRectF(2.5, 8.6, 11, 0.9))                       # seam
        p.setBrush(QColor("#57AB5A" if not dim else "#4A4A4A"))
        p.drawEllipse(QRectF(11, 9.8, 1.8, 1.8))                    # activity LED
    p.end()
    icon = _icon_cache[key] = QIcon(pm)
    return icon


# =========================================================================== worker thread
class EngineWorker(QObject):
    """Lives on its own QThread. Each slot makes one blocking C call; ctypes drops
    the GIL during the call, so the GUI thread keeps running at full speed."""

    # int64 values go through `object` so Qt does not squeeze them into a 32-bit int
    progress = Signal(int, object, object, object, object, object, str)
    finished = Signal(str, int, str)          # job name, DV_* code, detail text

    def __init__(self, engine: DiskEngine):
        super().__init__()
        self.engine = engine
        # The C library keeps a raw function pointer while a job runs: this
        # CFUNCTYPE object must stay referenced, or Python frees the thunk.
        self._cb = PROGRESS_FN(self._on_progress)

    def _on_progress(self, phase, files, dirs, nbytes, done, total, path):
        # Called by C on this worker thread with the GIL re-acquired by ctypes.
        # Emitting a signal across threads is queued, so the GUI updates safely.
        try:
            self.progress.emit(phase, files, dirs, nbytes, done, total, _u8(path))
        except Exception:
            pass                               # never let an exception unwind into C

    def _finish(self, job, code):
        self.finished.emit(job, code, self.engine.last_error() if code != DV_OK else "")

    @Slot(object, str, str)
    def scan(self, roots, label, db_path):
        self._finish("scan", self.engine.scan(roots, label, db_path, self._cb))

    @Slot(object, str, str)
    def refresh(self, roots, label, db_path):
        self._finish("refresh", self.engine.refresh(roots, label, db_path, self._cb))

    @Slot(str)
    def load(self, db_path):
        self._finish("load", self.engine.load_db(db_path, self._cb))

    @Slot(str)
    def save(self, db_path):
        self._finish("save", self.engine.save_db(db_path, self._cb))


# =========================================================================== tree model
class Node:
    """Python-side cache of one C node, so paint/data() never calls into C."""
    __slots__ = ("idx", "parent", "row", "name", "size", "mtime", "is_dir", "cat",
                 "flags", "child_count", "file_count", "kind", "kids")


class DiskTreeModel(QAbstractItemModel):
    """Custom model over the C index. Children are pulled lazily, in batches,
    through canFetchMore/fetchMore — expanding a folder with 100 000 entries
    only materialises the rows the view actually needs."""

    HEADERS = ("Name", "Size", "% of parent", "Type", "Modified")
    BATCH = 400
    PCT_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = None
        self._top = None

    def set_engine(self, engine):
        self.engine = engine

    def clear(self):
        self.beginResetModel()
        self._top = None
        self.endResetModel()

    def reload(self):
        self.beginResetModel()
        self._top = self._make(0, None, 0) if self.engine and self.engine.has_data() else None
        self.endResetModel()

    def _make(self, idx, parent, row):
        info = self.engine.info(idx)
        n = Node()
        n.idx, n.parent, n.row = idx, parent, row
        n.size = info.size if info else 0
        n.mtime = info.mtime if info else 0
        n.is_dir = bool(info.is_dir) if info else False
        n.cat = info.category if info else 0
        n.flags = info.flags if info else 0
        n.child_count = info.child_count if info else 0
        n.file_count = info.file_count if info else 0
        if n.flags & FLAG_VIRTUAL:
            n.kind = KIND_PC
        elif parent is not None and parent.kind == KIND_PC:
            n.kind = KIND_DRIVE
        else:
            n.kind = KIND_ITEM
        raw = self.engine.name(idx) if info else "?"
        n.name = drive_label(raw) if n.kind == KIND_DRIVE else raw
        n.kids = [] if n.is_dir else None
        return n

    def _index_of(self, node):
        return self.createIndex(node.row, 0, node)

    def _load_until(self, node, count):
        """Make sure the first `count` children of `node` exist as rows."""
        if node.kids is None:
            return
        count = min(count, node.child_count)
        start = len(node.kids)
        if start >= count:
            return
        ids = self.engine.children(node.idx, start, count - start)
        if not ids:
            return
        self.beginInsertRows(self._index_of(node), start, start + len(ids) - 1)
        node.kids.extend(self._make(i, node, start + k) for k, i in enumerate(ids))
        self.endInsertRows()

    def node_at(self, index):
        return index.internalPointer() if index.isValid() else None

    def top_index(self):
        return self.createIndex(0, 0, self._top) if self._top else QModelIndex()

    def ensure_index(self, idx):
        """Load every ancestor of C node `idx` and return its model index
        (used when the treemap asks to reveal an item)."""
        if not self._top or not self.engine:
            return QModelIndex()
        chain, i = [], idx
        while i >= 0 and len(chain) < 10000:
            chain.append(i)
            info = self.engine.info(i)
            if info is None:
                return QModelIndex()
            i = info.parent
        chain.reverse()
        if chain[0] != self._top.idx:
            return QModelIndex()
        node = self._top
        for child in chain[1:]:
            info = self.engine.info(child)
            if info is None:
                return QModelIndex()
            self._load_until(node, info.rank + 1)
            if node.kids is None or info.rank >= len(node.kids) or node.kids[info.rank].idx != child:
                return QModelIndex()
            node = node.kids[info.rank]
        return self._index_of(node)

    # ---- QAbstractItemModel interface ---------------------------------------
    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or not 0 <= column < len(self.HEADERS):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, self._top) if self._top and row == 0 else QModelIndex()
        kids = parent.internalPointer().kids
        if kids is None or row >= len(kids):
            return QModelIndex()
        return self.createIndex(row, column, kids[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        p = index.internalPointer().parent
        return QModelIndex() if p is None else self.createIndex(p.row, 0, p)

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return 1 if self._top else 0
        if parent.column() > 0:
            return 0
        kids = parent.internalPointer().kids
        return len(kids) if kids is not None else 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def hasChildren(self, parent=QModelIndex()):
        if not parent.isValid():
            return self._top is not None
        if parent.column() > 0:
            return False
        n = parent.internalPointer()
        return n.is_dir and n.child_count > 0

    def canFetchMore(self, parent):
        if not parent.isValid():
            return False
        n = parent.internalPointer()
        return n.kids is not None and len(n.kids) < n.child_count

    def fetchMore(self, parent):
        if parent.isValid():
            n = parent.internalPointer()
            self._load_until(n, len(n.kids) + self.BATCH)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        n, col = index.internalPointer(), index.column()
        problem = bool(n.flags & FLAG_PROBLEM)
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return n.name
            if col == 1:
                return fmt_size(n.size)
            if col == 2:
                return f"{self._pct(n) * 100:.1f} %"
            if col == 3:
                return type_text(n.name, n.is_dir, n.cat, n.file_count, n.kind)
            if col == 4:
                return "" if n.kind == KIND_PC else fmt_time(n.mtime)
        elif role == Qt.ItemDataRole.DecorationRole and col == 0:
            if n.kind != KIND_ITEM:
                return kind_icon(n.kind, dim=problem)
            return category_icon(n.cat if not n.is_dir else CAT_FOLDER, dim=problem)
        elif role == Qt.ItemDataRole.ForegroundRole and problem:
            return QColor(C_MUTED)
        elif role == Qt.ItemDataRole.ToolTipRole and col == 0:
            if n.kind == KIND_DRIVE and not problem:
                return self.engine.name(n.idx)
            return flag_text(n.flags) or None
        elif role == Qt.ItemDataRole.TextAlignmentRole and col in (1, 2):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == self.PCT_ROLE:
            return self._pct(n)
        elif role == Qt.ItemDataRole.UserRole:
            return n.idx
        return None

    @staticmethod
    def _pct(n):
        if n.parent is None:
            return 1.0
        return n.size / n.parent.size if n.parent.size > 0 else 0.0


class PercentDelegate(QStyledItemDelegate):
    """Draws the '% of parent' column as a small bar with the value on top."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        paint_row_hover(painter, opt, index, self.parent())    # tables: whole-row hover
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        frac = max(0.0, min(1.0, float(index.data(DiskTreeModel.PCT_ROLE) or 0.0)))
        r = opt.rect.adjusted(4, 5, -4, -5)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#333638"))
        painter.drawRect(r)
        if frac > 0:
            painter.setBrush(QColor(C_ACCENT))
            painter.drawRect(QRectF(r.x(), r.y(), max(1.0, r.width() * frac), r.height()))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(r.adjusted(0, 0, -3, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


# =========================================================================== treemap
class TreemapView(QGraphicsView):
    """Squarified treemap on a QGraphicsScene. Geometry comes from dv_treemap()
    in C; each rectangle becomes a QGraphicsRectItem tagged with its node index."""

    nodeClicked = Signal(int)
    rootChanged = Signal(int)

    MAX_RECTS = 15000
    MAX_DEPTH = 12
    MIN_SIDE = 3.0
    HEADER = 15.0
    PAD = 2.0
    MAX_LABELS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = None
        self.root = -1
        self.geom = {}                 # node -> (x, y, w, h) for the current layout
        self.selected = -1
        self.hovered = -1              # node under the mouse (hover outline)
        self._whole = False
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setBackgroundBrush(QColor(C_BODY))
        self.setMouseTracking(True)
        self.setMinimumHeight(120)
        # One reusable C-compatible output array: dv_treemap writes DvRect structs
        # straight into it, and we read the fields back as Python attributes.
        self._out = (DvRect * self.MAX_RECTS)()
        self._relayout_timer = QTimer(self, singleShot=True, interval=120, timeout=self.relayout)

        self._file_brush = [QBrush(QColor(c)) for _n, c in CATEGORIES]
        self._file_pen = [QPen(QColor(c).darker(170), 0) for _n, c in CATEGORIES]
        self._dir_brush = [QBrush(QColor("#22262D")), QBrush(QColor("#2A2F37"))]
        self._dir_full_brush = QBrush(QColor(CATEGORIES[CAT_FOLDER][1]))   # folder drawn as a block
        self._dir_pen = QPen(QColor("#454C57"), 0)
        self._font = QFont("Segoe UI", 8)
        self._fm = QFontMetrics(self._font)
        self._label_dark = QBrush(QColor("#111316"))
        self._label_light = QBrush(QColor("#D7DAE0"))

    def set_engine(self, engine):
        self.engine = engine

    def clear(self):
        self.root, self.geom, self.selected, self.hovered = -1, {}, -1, -1
        self._whole = False
        self._scene.clear()

    def set_root(self, idx):
        self.root = idx
        self.relayout()
        self.rootChanged.emit(idx)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._relayout_timer.start()           # size may have changed while hidden

    def relayout(self):
        self._scene.clear()
        self.geom = {}
        self.hovered = -1              # the next mouse move picks the new block
        vw, vh = self.viewport().width(), self.viewport().height()
        self._scene.setSceneRect(0, 0, vw, vh)
        if self.root < 0 or self.engine is None or vw < 8 or vh < 8:
            return
        n = self.engine.treemap(self.root, 0.0, 0.0, float(vw), float(vh), self.MAX_DEPTH,
                                self.MIN_SIDE, self.HEADER, self.PAD, self._out)
        if n <= 0:
            return
        add_rect, labels = self._scene.addRect, 0
        for k in range(n):
            r = self._out[k]                       # a DvRect view into the C-filled array
            node, x, y, w, h = r.node, r.x, r.y, r.w, r.h
            self.geom[node] = (x, y, w, h)
            if r.is_dir:
                brush = self._dir_brush[r.depth & 1] if r.expanded else self._dir_full_brush
                item = add_rect(x, y, w, h, self._dir_pen, brush)
            else:
                item = add_rect(x, y, w, h, self._file_pen[r.category], self._file_brush[r.category])
            item.setData(0, node)
            item.setZValue(r.depth)
            if labels < self.MAX_LABELS:
                if r.is_dir and r.expanded and h >= self.HEADER * 2.5 and w >= 48:
                    self._label(item, x + 4, y + 1, w - 8, f"{self.engine.label(node)}   {fmt_size(r.size)}",
                                self._label_light)
                    labels += 1
                elif not r.expanded and w >= 64 and h >= 18:
                    self._label(item, x + 3, y + 2, w - 6, self.engine.label(node),
                                self._label_light if r.is_dir else self._label_dark)
                    labels += 1

    def _label(self, parent_item, x, y, max_w, text, brush):
        text = self._fm.elidedText(text, Qt.TextElideMode.ElideRight, int(max_w))
        if not text or text == "…":
            return
        t = QGraphicsSimpleTextItem(text, parent_item)
        t.setFont(self._font)
        t.setBrush(brush)
        t.setPos(x, y)

    # ---- selection highlight -------------------------------------------------
    # The outline is painted in drawForeground() instead of being added to the
    # scene as items: relayout() calls scene.clear(), which deletes every item
    # in C++, and Python references to deleted highlight items crashed the app.
    def highlight(self, idx) -> bool:
        """Outline node idx if it is drawn in the current layout."""
        self.selected = idx
        self._whole = False
        self.viewport().update()
        return idx in self.geom

    def highlight_whole(self, idx):
        """The layout root itself is selected: outline the whole canvas."""
        self.selected = idx
        self._whole = True
        self.viewport().update()

    def set_hovered(self, node):
        if node != self.hovered:
            self.hovered = node
            self.viewport().update()

    def drawForeground(self, painter, rect):
        # hover: a light veil + thin outline on the block under the mouse
        if self.hovered >= 0 and self.hovered != self.selected and self.hovered in self.geom:
            hx, hy, hw, hh = self.geom[self.hovered]
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 40))
            painter.drawRect(QRectF(hx, hy, hw, hh))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 170), 1))
            painter.drawRect(QRectF(hx + 0.5, hy + 0.5, max(hw - 1, 1), max(hh - 1, 1)))
            painter.restore()
        if self.selected < 0:
            return
        if self._whole and self.selected == self.root:
            r = self._scene.sceneRect()
            x, y, w, h = r.x(), r.y(), r.width(), r.height()
        elif self.selected in self.geom:
            x, y, w, h = self.geom[self.selected]
        else:
            return
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 46))
        painter.drawRect(QRectF(x, y, w, h))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#000000"), 0))
        painter.drawRect(QRectF(x, y, w, h))
        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        painter.drawRect(QRectF(x + 1, y + 1, max(w - 2, 1), max(h - 2, 1)))
        painter.restore()

    # ---- mouse ---------------------------------------------------------------
    def node_at(self, pos) -> int:
        """Deepest treemap node under a viewport position (ignores overlays)."""
        for item in self.items(pos):              # sorted top-most first
            while item is not None and item.data(0) is None:
                item = item.parentItem()          # label text → its rectangle
            if item is not None:
                return int(item.data(0))
        return -1

    def mouseMoveEvent(self, event):
        self.set_hovered(self.node_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        node = self.node_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and node >= 0:
            self.highlight(node)
            self.nodeClicked.emit(node)
        elif event.button() == Qt.MouseButton.RightButton:
            self.zoom_out()
        event.accept()

    def _zoom_target(self, node) -> int:
        """Folder to zoom into for a double-click on `node`: walk up from the
        block under the cursor (usually a file deep inside the folder) to the
        folder that is a direct child of the current treemap root."""
        target = -1
        i = node
        while i >= 0 and i != self.root:
            info = self.engine.info(i)
            if info is None:
                return -1
            if info.parent == self.root:
                if info.is_dir and info.child_count > 0:
                    target = i
                break
            i = info.parent
        return target

    def mouseDoubleClickEvent(self, event):
        node = self.node_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and node >= 0 and self.engine:
            folder = self._zoom_target(node)
            if folder >= 0:
                self.set_root(folder)           # zoom one level into that folder
                if node == folder or not self.highlight(node):
                    self.highlight_whole(folder)
                self.nodeClicked.emit(node if node in self.geom else folder)
        event.accept()

    def zoom_out(self):
        if self.root >= 0 and self.engine:
            info = self.engine.info(self.root)
            if info and info.parent >= 0:
                old = self.root
                self.set_root(info.parent)
                self.highlight(old)
                self.nodeClicked.emit(old)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.set_hovered(-1)
        if event.type() == QEvent.Type.ToolTip and self.engine:
            node = self.node_at(event.pos())
            if node >= 0:
                info = self.engine.info(node)
                if info:
                    kind = self.engine.kind(node, info)
                    name = self.engine.label(node, info)
                    lines = [f"<b>{name}</b>",
                             f"{fmt_size(info.size)} · {type_text(name, info.is_dir, info.category, info.file_count, kind)}",
                             f"<span style='color:{C_MUTED}'>{self.engine.path(node)}</span>"]
                    if info.flags & FLAG_PROBLEM:
                        lines.append(flag_text(info.flags))
                    QToolTip.showText(event.globalPos(), "<br>".join(lines), self)
                    return True
            QToolTip.hideText()
            return True
        return super().viewportEvent(event)


# =========================================================================== progress ring
class RingProgress(QWidget):
    """Circular progress. value in [0, 1], or None for an indeterminate spinning arc."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(160, 160)
        self._value = None
        self._text = ""
        self._angle = 0
        self._timer = QTimer(self, interval=16, timeout=self._tick)

    def start(self):
        self._value, self._text, self._angle = None, "", 0
        self._timer.start()
        self.update()

    def stop(self):
        self._timer.stop()

    def set_progress(self, value, text):
        self._value = None if value is None else max(0.0, min(1.0, value))
        self._text = text
        self.update()

    def _tick(self):
        self._angle = (self._angle + 6) % 360
        if self._value is None:
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        thick = 10
        r = QRectF(thick, thick, self.width() - 2 * thick, self.height() - 2 * thick)
        pen = QPen(QColor("#333638"), thick)
        p.setPen(pen)
        p.drawEllipse(r)
        pen.setColor(QColor(C_ACCENT))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if self._value is None:
            p.drawArc(r, int(-self._angle * 16), 90 * 16)          # spinning segment
        elif self._value > 0:
            p.drawArc(r, 90 * 16, int(-self._value * 360 * 16))     # clockwise from 12 o'clock
        p.setPen(QColor("#FFFFFF"))
        f = QFont(self.font())
        f.setPointSize(15)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class BusyPanel(QWidget):
    """Shown in place of the tree + treemap while a scan / load runs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        self.ring = RingProgress()
        lay.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(14)
        self.title = QLabel("")
        self.title.setObjectName("busyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)
        self.detail = QLabel("")
        self.detail.setObjectName("dim")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.detail)
        self.path = QLabel("")
        self.path.setObjectName("dim")
        self.path.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.path)
        lay.addStretch(1)

    def start(self, title):
        self.title.setText(title)
        self.detail.setText("")
        self.path.setText("")
        self.ring.start()

    def stop(self):
        self.ring.stop()

    def set_progress(self, frac, center, detail, path=""):
        text = f"{frac * 100:.0f}%" if frac is not None else (center or "")
        self.ring.set_progress(frac, text)
        self.detail.setText(detail)
        if path:
            fm = self.path.fontMetrics()
            self.path.setText(fm.elidedText(path, Qt.TextElideMode.ElideMiddle,
                                            max(self.width() - 40, 200)))
        else:
            self.path.setText("")


def make_info_icon(size: int = 32, color: str = C_TEXT) -> QPixmap:
    """A round "i" icon, drawn by hand (no image file needed)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    pen = QPen(QColor(color), max(1.5, s * 0.08))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    m = s * 0.1
    p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))      # ring
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    d = s * 0.13
    p.drawEllipse(QRectF(s / 2 - d / 2, s * 0.25, d, d))   # dot
    w = s * 0.11
    p.drawRoundedRect(QRectF(s / 2 - w / 2, s * 0.44, w, s * 0.32), w / 2, w / 2)  # stem
    p.end()
    return pm


EXPLORER_HELP_HTML = f"""
<h3 style='margin:0 0 6px 0'>How to use Disk Visualizer</h3>
<p style='color:{C_MUTED}; margin:0 0 10px 0'>Double-click a box to open a folder, right-click to go back up one level (or use the <b>▲ Up</b> button), click the arrows next to rows in the list to open or close folders, and click <b>Refresh</b> to rescanned updated drives.</p>
"""


def show_help_dialog(parent, html: str, title: str = "How to use"):
    """The "i" button dialog — shared by all pages."""
    dlg = QDialog(parent)
    dlg.setObjectName("help")
    dlg.setWindowTitle(title)
    dlg.setWindowIcon(parent.window().windowIcon())
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(20, 18, 20, 14)
    lay.setSpacing(12)
    row = QHBoxLayout()
    row.setSpacing(16)
    icon = QLabel()
    icon.setPixmap(make_info_icon(40, C_ACCENT))
    icon.setAlignment(Qt.AlignmentFlag.AlignTop)
    row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    text = QLabel(html)
    text.setObjectName("helpText")
    text.setTextFormat(Qt.TextFormat.RichText)
    text.setWordWrap(True)
    text.setMinimumWidth(560)
    row.addWidget(text, 1)
    lay.addLayout(row)
    ok = QPushButton("OK")
    ok.setObjectName("primary")
    ok.setFixedWidth(80)
    ok.setDefault(True)
    ok.clicked.connect(dlg.accept)
    lay.addWidget(ok, 0, Qt.AlignmentFlag.AlignRight)
    dlg.exec()


# =========================================================================== explorer page
def legend_html() -> str:
    return "&nbsp;&nbsp;".join(
        f"<span style='color:{c}'>■</span>&nbsp;{n}" for n, c in CATEGORIES)


class PageBase(QWidget):
    """What every page has in common: the top bar with its Refresh and Info
    buttons, the engine handle, the busy state, and the "refill when shown"
    flag. Each Refresh button is wired to the same handler by the window, so
    pressing Refresh anywhere does one thing. Subclasses add their body to
    ``self.lay`` and override ``refresh()`` to redraw themselves.

    This used to be copied into ExplorerPage and ListPage separately, which is
    how the two Refresh buttons drifted apart in tooltip and enabled state."""

    HELP_TITLE = "How to use"
    HELP_HTML = ""
    # No Scan button anywhere: the app reads the drives by itself on start-up —
    # a full scan the first time, an update every time after that.
    REFRESH_TIP = "Re-read the drives and bring this page up to date"
    INFO_TIP = "How to use this page"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.engine = None
        self._dirty = True

        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(10, 8, 10, 10)
        self.lay.setSpacing(6)
        self.lay.addLayout(self._make_bar())

    def _make_bar(self):
        bar = QHBoxLayout()
        bar.setSpacing(6)
        head = self.make_head()
        if head is not None:
            bar.addWidget(head, 1)
        else:
            bar.addStretch(1)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip(self.REFRESH_TIP)
        self.btn_refresh.setFixedHeight(28)
        bar.addWidget(self.btn_refresh)
        self.btn_info = QPushButton()
        self.btn_info.setObjectName("info")
        self.btn_info.setIcon(QIcon(make_info_icon(32)))
        self.btn_info.setToolTip(self.INFO_TIP)
        self.btn_info.setFixedSize(30, 28)
        self.btn_info.clicked.connect(
            lambda: show_help_dialog(self, self.HELP_HTML, self.HELP_TITLE))
        bar.addWidget(self.btn_info)
        return bar

    def make_head(self):
        """Optional widget on the left of the bar (filters, counts…)."""
        return None

    def set_engine(self, engine):
        self.engine = engine
        self.btn_refresh.setEnabled(engine is not None)

    def set_busy(self, busy):
        self.btn_refresh.setEnabled(not busy and self.engine is not None)

    def invalidate(self):
        """The index changed (scan, refresh or load): redraw now if this page is
        on screen, otherwise the next time it is shown."""
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            QTimer.singleShot(0, self.refresh)

    def refresh(self):
        """Redraw from the current index. Pages that draw themselves override
        this; the Explorer is drawn by the window instead."""
        self._dirty = False


class ExplorerPage(PageBase):
    HELP_HTML = EXPLORER_HELP_HTML
    REFRESH_TIP = "Re-read the drives and bring the view up to date"
    INFO_TIP = "How to use"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = self.lay

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)

        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setItemDelegateForColumn(2, PercentDelegate(self.tree))
        split.addWidget(self.tree)

        tm_box = QWidget()
        tl = QVBoxLayout(tm_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        head = QHBoxLayout()
        self.btn_up = QPushButton("▲ Up")
        self.btn_up.setToolTip("Zoom out one level (or right-click the treemap)")
        self.btn_up.setEnabled(False)
        head.addWidget(self.btn_up)
        self.crumb = QLabel("")
        self.crumb.setObjectName("crumb")
        self.crumb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.crumb, 1)
        legend = QLabel(legend_html())
        legend.setObjectName("dim")
        head.addWidget(legend)
        tl.addLayout(head)
        self.treemap = TreemapView()
        tl.addWidget(self.treemap, 1)
        split.addWidget(tm_box)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 5)

        self.body = QStackedWidget()
        self.body.addWidget(split)               # 0 = tree + treemap
        self.busy_panel = BusyPanel()
        self.body.addWidget(self.busy_panel)     # 1 = round progress
        lay.addWidget(self.body, 1)


# =========================================================================== list pages
# Pages 2–4: Largest files, Old files, Junk folders. All lists come from the
# C index (dv_search / dv_find_old / dv_find_named_dirs) of the last scan.

JUNK_NAMES = ("node_modules", "__pycache__", ".gradle", "Temp", "Cache", "$Recycle.Bin", "Windows.old", ".venv")

# lower-case folder name → what it is (shown as the folder's tooltip)
JUNK_ABOUT = {
    "node_modules": "JavaScript packages. Get them back with “npm install”.",
    "__pycache__": "Python's compiled-code cache. Python rebuilds it by itself.",
    ".gradle": "Gradle build cache. It is rebuilt on the next build.",
    ".venv": "Python virtual environment. Recreate it with “pip install -r requirements.txt”.",
    "cache": "A program's cache. The program rebuilds it when needed.",
    "temp": "Temporary files. Programs expect the folder itself to exist — delete only what is inside.",
    "$recycle.bin": "Files that are already in the Recycle Bin. Empty the bin to free this space.",
    "windows.old": "A previous Windows installation. Remove it with Windows Disk Cleanup.",
}

OLD_DAYS = 365          # "old" = not changed or opened for 1 year
OLD_MIN_SIZE = 0        # any size

OPEN_LABEL = "Open in File Explorer" if sys.platform == "win32" else "Open in file manager"
C_HOVER_ROW = "#2A2D2E"
C_JUNK_TEXT = "#F14C4C"

LARGEST_HELP_HTML = f"""
<h3 style='margin:0 0 6px 0'>Largest files</h3>
<p style='color:{C_MUTED}; margin:0 0 10px 0'>Shows the 100 biggest files across your drives.</p>
<table cellspacing='0' cellpadding='4'>
<tr><td><b>% of disk</b></td><td>Percentage of your total disk space this file takes.</td></tr>
<tr><td><b>{OPEN_LABEL}</b></td><td>Opens the containing folder with the file selected.</td></tr>
</table>
"""

OLD_HELP_HTML = f"""
<h3 style='margin:0 0 6px 0'>Old files</h3>
<p style='color:{C_MUTED}; margin:0 0 10px 0'>Shows files unedited or unopened for over 1 year, largest first.</p>
<table cellspacing='0' cellpadding='4'>
<tr><td><b>Last opened</b></td><td>Uses the newest date available so files never appear older than they are.</td></tr>
<tr><td><b>{OPEN_LABEL}</b></td><td>Opens the containing folder with the file selected.</td></tr>
</table>
"""

JUNK_HELP_HTML = f"""
<h3 style='margin:0 0 6px 0'>Junk folders</h3>
<p style='color:{C_MUTED}; margin:0 0 10px 0'>Shows temporary or rebuildable folders generated by apps.</p>
<table cellspacing='0' cellpadding='4'>
<tr><td><b>Temp / $Recycle.Bin</b></td><td>Empty the Recycle Bin or clean Temp contents, do not delete the folders.</td></tr>
<tr><td><b>Windows.old</b></td><td>Use Windows Disk Cleanup → “Clean up system files”.</td></tr>
<tr><td><b>{OPEN_LABEL}</b></td><td>Opens the parent folder with the item selected. Hover a name to see details.</td></tr>
</table>
"""

def paint_row_hover(painter, opt, index, view) -> None:
    """Shared by the table delegates: drop the per-cell hover look and paint
    the hover colour across the whole row the mouse is on instead."""
    hover_row = getattr(view, "hover_row", None)
    if hover_row is None:
        return
    opt.state &= ~QStyle.StateFlag.State_MouseOver
    if index.row() == hover_row and not (opt.state & QStyle.StateFlag.State_Selected):
        painter.fillRect(opt.rect, QColor(C_HOVER_ROW))


class RowHoverDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        paint_row_hover(painter, opt, index, self.parent())
        super().paint(painter, opt, index)


class HoverTable(QTableWidget):
    """QTableWidget that highlights the whole row under the mouse (not just
    the cell), including while the mouse is over a row's button."""

    def __init__(self, rows, cols, parent=None):
        super().__init__(rows, cols, parent)
        self.hover_row = -1
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(RowHoverDelegate(self))

    def set_hover(self, row):
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()

    def mouseMoveEvent(self, event):
        self.set_hover(self.rowAt(int(event.position().y())))
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.set_hover(-1)
        return super().viewportEvent(event)

    def wheelEvent(self, event):
        super().wheelEvent(event)
        self.set_hover(self.rowAt(int(event.position().y())))

    def track(self, widget):
        """Cell widgets swallow mouse events: watch them so their row lights up too."""
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove) and isinstance(obj, QWidget):
            try:
                p = obj.mapTo(self.viewport(), QPoint(1, 1))
            except RuntimeError:
                return False
            self.set_hover(self.rowAt(p.y()))
        return False

    def clear_rows(self, count):
        self.hover_row = -1
        self.setRowCount(0)
        self.setRowCount(count)


def make_table(headers, widths) -> HoverTable:
    """widths: >0 fixed start width, 0 = stretch, -1 = the "Open in File Explorer" button column."""
    t = HoverTable(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    t.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(30)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    for c, w in enumerate(widths):
        if w == 0:
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        elif w < 0:
            # a cell widget does not count for ResizeToContents → size it for the button
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(c, t.fontMetrics().horizontalAdvance(OPEN_LABEL) + 24 + 2 * 22)
        else:
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            t.setColumnWidth(c, w)
    return t


def cell_text(text, right=False, tip=None):
    it = QTableWidgetItem(text)
    if right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tip:
        it.setToolTip(tip)
    return it


def cell_name(name, info, tip):
    icon = category_icon(CAT_FOLDER if info.is_dir else info.category)
    it = QTableWidgetItem(icon, name)
    it.setToolTip(tip)
    return it


def open_cell(table, path, is_dir=False):
    """The single action button: open the parent folder with the item selected."""
    cell = QWidget()
    lay = QHBoxLayout(cell)
    lay.setContentsMargins(4, 2, 4, 2)
    b = QPushButton(OPEN_LABEL)
    b.setObjectName("rowBtn")
    b.setToolTip(path)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setMinimumWidth(b.fontMetrics().horizontalAdvance(OPEN_LABEL) + 24)
    b.clicked.connect(lambda _checked=False: show_in_file_manager(path, is_dir))
    lay.addWidget(b)
    lay.addStretch(1)
    table.track(cell)
    return cell


class ListPage(PageBase):
    """Shared frame for pages 2–4: PageBase's top row (optional line + Refresh
    + the "i" button) and the table, or a hint when there is no scan yet."""

    REFRESH_TIP = "Re-read the drives and bring this list up to date"
    HEADERS = ()
    WIDTHS = ()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = self.lay

        self.outer = QStackedWidget()
        empty = QLabel("Nothing to show yet.\n\nThe Explorer page is still reading your drives.")
        empty.setObjectName("dim")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.outer.addWidget(empty)                  # 0 = no scan data
        self.table = make_table(self.HEADERS, self.WIDTHS)
        self.outer.addWidget(self.table)             # 1 = the list
        lay.addWidget(self.outer, 1)

    def set_busy(self, busy):
        super().set_busy(busy)
        self.table.setEnabled(not busy)

    def refresh(self):
        super().refresh()
        e = self.engine
        if e is None or not e.has_data():
            self.outer.setCurrentIndex(0)
            self.no_data()
            return
        self.outer.setCurrentIndex(1)
        self.fill()

    def no_data(self):
        pass

    def fill(self):
        raise NotImplementedError


class LargestPage(ListPage):
    HELP_TITLE = "Largest files"
    HELP_HTML = LARGEST_HELP_HTML
    HEADERS = ("Name", "Size", "% of disk", "Folder", "Action")
    WIDTHS = (260, 90, 130, 0, -1)
    TOP_N = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table.setItemDelegateForColumn(2, PercentDelegate(self.table))

    def _all_drives_total(self) -> int:
        """Combined capacity of every scanned drive (for "% of disk")."""
        total = 0
        for idx in self.engine.drives():
            try:
                total += shutil.disk_usage(self.engine.path(idx)).total
            except OSError:
                pass
        return total

    def fill(self):
        e, t = self.engine, self.table
        ids, _total = e.search("", ALL_CATS, 0, 1, self.TOP_N)     # kind=1 → files only
        rows = [(i, e.info(i)) for i in ids]
        rows = [(i, inf) for i, inf in rows if inf is not None and inf.size > 0]
        denom = self._all_drives_total() or e.stats().bytes
        t.clear_rows(len(rows))
        for r, (idx, info) in enumerate(rows):
            name, path = e.name(idx), e.path(idx)
            frac = info.size / denom if denom > 0 else 0.0
            pct = cell_text(f"{frac * 100:.2f} %", right=True)
            pct.setData(DiskTreeModel.PCT_ROLE, frac)      # PercentDelegate draws the bar
            t.setItem(r, 0, cell_name(name, info, path))
            t.setItem(r, 1, cell_text(fmt_size(info.size), right=True))
            t.setItem(r, 2, pct)
            t.setItem(r, 3, cell_text(os.path.dirname(path), tip=path))
            t.setCellWidget(r, 4, open_cell(t, path))


class OldPage(ListPage):
    HELP_TITLE = "Old files"
    HELP_HTML = OLD_HELP_HTML
    HEADERS = ("Name", "Size", "Last changed", "Last opened", "Folder", "Action")
    WIDTHS = (240, 90, 125, 125, 0, -1)
    OLD_N = 200

    def fill(self):
        e, t = self.engine, self.table
        cutoff = int(time.time()) - OLD_DAYS * 86400
        ids, _total, _nbytes = e.find_old(cutoff, OLD_MIN_SIZE, self.OLD_N)
        t.clear_rows(len(ids))
        for r, idx in enumerate(ids):
            info = e.info(idx)
            if info is None:
                continue
            name, path = e.name(idx), e.path(idx)
            t.setItem(r, 0, cell_name(name, info, path))
            t.setItem(r, 1, cell_text(fmt_size(info.size), right=True))
            t.setItem(r, 2, cell_text(fmt_time(info.mtime)))
            t.setItem(r, 3, cell_text(fmt_time(info.atime) or "—"))
            t.setItem(r, 4, cell_text(os.path.dirname(path), tip=path))
            t.setCellWidget(r, 5, open_cell(t, path))


class JunkPage(ListPage):
    HELP_TITLE = "Junk folders"
    HELP_HTML = JUNK_HELP_HTML
    HEADERS = ("Folder", "Location", "Items", "Size", "Action")
    WIDTHS = (140, 0, 110, 90, -1)
    JUNK_N = 300

    def make_head(self):
        self.banner = QLabel("")
        self.banner.setObjectName("junkLine")
        self.banner.setWordWrap(False)
        self.banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return self.banner

    def no_data(self):
        self.banner.setText("")

    def fill(self):
        e, t = self.engine, self.table
        ids, total, nbytes = e.find_named_dirs(JUNK_NAMES, self.JUNK_N)
        t.clear_rows(len(ids))
        for r, idx in enumerate(ids):
            info = e.info(idx)
            if info is None:
                continue
            name, path = e.name(idx), e.path(idx)
            files = info.file_count
            nm = QTableWidgetItem(category_icon(CAT_FOLDER), name)
            nm.setToolTip(JUNK_ABOUT.get(name.lower(), ""))
            t.setItem(r, 0, nm)
            t.setItem(r, 1, cell_text(path, tip=path))
            t.setItem(r, 2, cell_text(f"{files:,} file{'s' if files != 1 else ''}", right=True))
            t.setItem(r, 3, cell_text(fmt_size(info.size), right=True))
            t.setCellWidget(r, 4, open_cell(t, path, True))
        if total:
            what = "this junk folder" if total == 1 else f"these {total:,} junk folders"
            text = (f"Deleting {what} could free up about {fmt_size(nbytes)} — "
                    f"programs can rebuild most of them when needed.")
        else:
            text = "No known junk folders were found on any drive."
        self.banner.setText(text)
        self.banner.setToolTip(text)


# =========================================================================== logo
def make_app_icon() -> QIcon:
    """Draws a small hard-disk platter logo at several sizes."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C_ACCENT))
        p.drawRoundedRect(0, 0, size, size, size * 0.2, size * 0.2)

        s = float(size)
        platter = QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(platter)
        # used-space wedge
        p.setBrush(QColor("#0B4F80"))
        p.drawPie(platter, 90 * 16, -120 * 16)
        # spindle
        hub = s * 0.14
        p.setBrush(QColor(C_ACCENT))
        p.drawEllipse(QRectF(s / 2 - hub / 2, s / 2 - hub / 2, hub, hub))
        p.end()
        icon.addPixmap(pm)
    return icon


# =========================================================================== title bar
class CaptionButton(QPushButton):
    """Minimise / maximise / restore / close button drawn by hand."""

    def __init__(self, kind: str, tooltip: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setToolTip(tooltip)
        self.setFixedSize(46, TITLEBAR_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_kind(self, kind: str):
        self.kind = kind
        self.setToolTip("Restore" if kind == "restore" else "Maximise")
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        hovered = self.underMouse()
        if hovered:
            bg = C_CLOSE_HOVER if self.kind == "close" else C_HOVER
        else:
            bg = C_TITLEBAR
        p.fillRect(self.rect(), QColor(bg))
        pen = QPen(QColor("#FFFFFF" if hovered else C_TEXT))
        pen.setWidth(1)
        p.setPen(pen)
        cx, cy = self.width() // 2, self.height() // 2
        if self.kind == "min":
            p.drawLine(cx - 5, cy, cx + 5, cy)
        elif self.kind == "max":
            p.drawRect(cx - 5, cy - 5, 10, 10)
        elif self.kind == "restore":
            p.drawRect(cx - 5, cy - 2, 8, 8)
            p.drawPolyline(QPolygon([
                QPoint(cx - 2, cy - 2), QPoint(cx - 2, cy - 5), QPoint(cx + 5, cy - 5),
                QPoint(cx + 5, cy + 2), QPoint(cx + 3, cy + 2),
            ]))
        elif self.kind == "close":
            # crisp 1-px X (no antialiasing) so it matches the min / max glyphs
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.drawLine(cx - 5, cy - 5, cx + 5, cy + 5)
            p.drawLine(cx - 5, cy + 5, cx + 5, cy - 5)


class TitleBar(QWidget):
    def __init__(self, window: "MainWindow", title: str):
        super().__init__(window)
        self._win = window
        self.setObjectName("titleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLEBAR_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        logo = QLabel()
        logo.setPixmap(window.windowIcon().pixmap(16, 16))
        logo.setContentsMargins(10, 0, 8, 0)
        logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(logo)

        text = QLabel(title)
        text.setObjectName("titleText")
        text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(text)

        lay.addStretch(1)

        self.btn_min = CaptionButton("min", "Minimise")
        self.btn_max = CaptionButton("max", "Maximise")
        self.btn_close = CaptionButton("close", "Close")
        self.btn_min.clicked.connect(window.showMinimized)
        self.btn_max.clicked.connect(window.toggle_max_restore)
        self.btn_close.clicked.connect(window.close)
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)

    def mousePressEvent(self, event):
        # drag the window by its title bar
        if event.button() == Qt.MouseButton.LeftButton and not self._win.isMaximized():
            handle = self._win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._win.toggle_max_restore()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


# =========================================================================== menu bar
class MenuBar(QWidget):
    def __init__(self, page_names, parent=None):
        super().__init__(parent)
        self.setObjectName("menuBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(TITLEBAR_HEIGHT)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(0)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, name in enumerate(page_names):
            btn = QPushButton(name)
            btn.setObjectName("menuBtn")
            btn.setCheckable(True)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.ArrowCursor)
            btn.setFixedHeight(TITLEBAR_HEIGHT)
            self.group.addButton(btn, i)
            lay.addWidget(btn)
        if page_names:
            self.group.button(0).setChecked(True)
        lay.addStretch(1)


# =========================================================================== style
STYLE = f"""
#root {{ background: {C_TITLEBAR}; }}

#titleBar {{ background: {C_TITLEBAR}; }}
#titleText {{ color: {C_TEXT}; font-size: 12px; background: transparent; padding-left: 2px; }}

#menuBar {{ background: #2D2D2D; border-top: 1px solid #1A1A1A; border-bottom: 1px solid #1A1A1A; }}
#menuBar QPushButton#menuBtn {{
    background: transparent; color: {C_TEXT}; border: none; border-radius: 0;
    border-bottom: 2px solid transparent; padding: 0 14px; font-size: 13px;
}}
#menuBar QPushButton#menuBtn:hover {{ background: {C_HOVER}; }}
#menuBar QPushButton#menuBtn:checked {{ color: #FFFFFF; border-bottom: 2px solid {C_ACCENT}; }}

#stack, #page {{ background: {C_BODY}; }}
#page QLabel {{ color: {C_TEXT}; }}
#page QLabel#dim {{ color: {C_MUTED}; }}
#page QLabel#crumb {{ color: #FFFFFF; padding-left: 6px; }}
#page QLabel#busyTitle {{ color: #FFFFFF; font-size: 14px; }}

QPushButton {{
    background: #3A3D41; color: {C_TEXT}; border: 1px solid #4A4D51; border-radius: 3px;
    padding: 4px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: #45494E; }}
QPushButton:pressed {{ background: #2F3236; }}
QPushButton:disabled {{ color: #6A6A6A; background: #2E3033; border-color: #3A3A3A; }}
QPushButton#primary {{ background: {C_ACCENT}; color: #FFFFFF; border-color: #1C8BD6; }}
QPushButton#primary:hover {{ background: #1C8BD6; }}
QPushButton#info {{ padding: 0; }}
QPushButton#primary:disabled {{ background: #264F6E; color: #9AA9B5; border-color: #264F6E; }}

QLineEdit, QComboBox {{
    background: #3C3C3C; color: {C_TEXT}; border: 1px solid #4A4A4A; border-radius: 3px;
    padding: 3px 6px; selection-background-color: {C_ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {C_ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {C_PANEL}; color: {C_TEXT}; border: 1px solid #454545;
    selection-background-color: {C_ACCENT}; outline: none;
}}

QTreeView {{
    background: {C_BODY}; alternate-background-color: #222324; color: {C_TEXT};
    border: 1px solid #333333; outline: none; selection-background-color: #094771;
    selection-color: #FFFFFF;
}}
QTreeView::item {{ padding: 2px 0; }}
QTreeView::item:selected {{ background: #094771; color: #FFFFFF; }}
QTreeView::item:hover {{ background: #2A2D2E; }}
QHeaderView::section {{
    background: #2D2D2D; color: {C_TEXT}; border: none; border-right: 1px solid #3A3A3A;
    border-bottom: 1px solid #3A3A3A; padding: 4px 6px;
}}

QSplitter::handle {{ background: {C_BODY}; }}
QSplitter::handle:hover {{ background: #333333; }}

QMenu {{ background: {C_PANEL}; color: {C_TEXT}; border: 1px solid #454545; padding: 4px 0; }}
QMenu::item {{ padding: 5px 22px; }}
QMenu::item:selected {{ background: {C_ACCENT}; color: #FFFFFF; }}
QMenu::item:disabled {{ color: #6A6A6A; }}

QMessageBox, QDialog#help {{ background: {C_PANEL}; }}
QDialog#help QLabel {{ color: {C_TEXT}; }}
QMessageBox QLabel {{ color: {C_TEXT}; }}

QScrollBar:vertical {{ background: {C_BODY}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #424242; min-height: 30px; border-radius: 3px; }}
QScrollBar::handle:vertical:hover {{ background: #555555; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {C_BODY}; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #424242; min-width: 30px; border-radius: 3px; }}
QScrollBar::handle:horizontal:hover {{ background: #555555; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QAbstractScrollArea::corner {{ background: {C_BODY}; }}
QToolTip {{ background: #252526; color: {C_TEXT}; border: 1px solid #454545; padding: 4px; }}

QPushButton#rowBtn {{ padding: 1px 10px; min-height: 0; }}
#page QLabel#junkLine {{ color: {C_JUNK_TEXT}; font-size: 13px; font-weight: 600; padding-left: 2px; }}
QTableView {{
    background: {C_BODY}; alternate-background-color: #222324; color: {C_TEXT};
    border: 1px solid #333333; outline: none; gridline-color: #333333;
    selection-background-color: #094771; selection-color: #FFFFFF;
}}
QTableView::item {{ padding: 0 4px; }}
QTableView::item:selected {{ background: #094771; color: #FFFFFF; }}
"""


# =========================================================================== window
class MainWindow(QWidget):
    # Requests to the worker thread (queued connections → run on the worker).
    sig_scan = Signal(object, str, str)        # list of drive roots, root label, db path
    sig_refresh = Signal(object, str, str)     # same, but reuses the index in RAM
    sig_load = Signal(str)
    sig_save = Signal(str)

    PAGE_NAMES = ("Explorer", "Largest files", "Old files", "Junk folders")

    def __init__(self, engine, engine_error):
        super().__init__()
        self.engine = engine
        self.engine_error = engine_error
        self.busy = False
        self._job_t0 = 0.0
        self._volume_used = 0
        self._syncing = False
        # Where the user is, as paths — node indices do not survive a refresh.
        self._last_root_path = ""
        self._last_sel_path = ""
        try:
            self.db_path = default_db_path()
        except OSError:
            self.db_path = os.path.join(_base_dir(), DB_NAME)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setObjectName("root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(760, 480)
        self.resize(1180, 760)

        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(0)
        self._layout.setContentsMargins(*(RESIZE_MARGIN,) * 4)

        self.titlebar = TitleBar(self, APP_NAME)
        self.titlebar.setCursor(Qt.CursorShape.ArrowCursor)
        self._layout.addWidget(self.titlebar)

        self.menubar = MenuBar(self.PAGE_NAMES, self)
        self._layout.addWidget(self.menubar)

        self.explorer = ExplorerPage()
        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        self.stack.setCursor(Qt.CursorShape.ArrowCursor)
        self.stack.addWidget(self.explorer)
        self.largest = LargestPage()
        self.old = OldPage()
        self.junk = JunkPage()
        self.list_pages = (self.largest, self.old, self.junk)
        self.pages = (self.explorer,) + self.list_pages   # every page, Explorer first
        for page in self.list_pages:
            self.stack.addWidget(page)
        self._layout.addWidget(self.stack, 1)

        self.menubar.group.idClicked.connect(self._on_page)
        self.setStyleSheet(STYLE)

        # ---- models & views
        self.model = DiskTreeModel(self)
        self.model.set_engine(engine)
        tree = self.explorer.tree
        tree.setModel(self.model)
        hdr = tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 90), (2, 130), (3, 170), (4, 130)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            tree.setColumnWidth(col, w)
        tree.selectionModel().currentChanged.connect(self._on_tree_current)
        tree.customContextMenuRequested.connect(self._tree_menu)

        self.treemap = self.explorer.treemap
        self.treemap.set_engine(engine)
        self.treemap.nodeClicked.connect(self._reveal_in_tree)
        self.treemap.rootChanged.connect(self._on_treemap_root)
        self.explorer.btn_up.clicked.connect(self.treemap.zoom_out)

        # One handler for every page's Refresh button, and Esc to cancel.
        # Each page wires its own Info button to its own help text.
        for page in self.pages:
            page.set_engine(engine)
            page.btn_refresh.clicked.connect(self._manual_refresh)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._cancel)

        # ---- worker thread
        self.thread = None
        if engine is not None:
            self.thread = QThread(self)
            self.worker = EngineWorker(engine)
            self.worker.moveToThread(self.thread)
            self.sig_scan.connect(self.worker.scan)
            self.sig_refresh.connect(self.worker.refresh)
            self.sig_load.connect(self.worker.load)
            self.sig_save.connect(self.worker.save)
            self.worker.progress.connect(self._on_progress)
            self.worker.finished.connect(self._on_finished)
            self.thread.start()
            self._load_saved()                     # no saved scan → full scan starts by itself
        else:
            QTimer.singleShot(0, lambda: QMessageBox.critical(self, APP_NAME, engine_error or "logic.dll failed to load"))

    # ------------------------------------------------------------------ jobs
    def _set_busy(self, busy, title=""):
        self.busy = busy
        e = self.explorer
        for page in self.pages:
            page.set_busy(busy)
        if busy:
            e.busy_panel.start(title)
            self._job_t0 = time.perf_counter()
        else:
            e.busy_panel.stop()
        e.body.setCurrentIndex(1 if busy else 0)

    def _clear_views(self):
        # Node indices are about to change: drop everything that holds them.
        self.model.clear()
        self.treemap.clear()
        self.explorer.btn_up.setEnabled(False)
        self.explorer.crumb.setText("")

    def _load_saved(self):
        self._set_busy(True, "Loading last scan…")
        self.sig_load.emit(self.db_path)

    def _start_job(self, full: bool):
        """Read the drives, either way: a full scan (full=True) or an update of
        the index already in RAM. Both put the same progress ring up and hand
        the same arguments to the worker, so they share one body — only the
        signal, the title, and whether the views have to be dropped first
        differ. Use the two wrappers below rather than calling this directly."""
        if self.engine is None or self.thread is None:
            return
        drives = list_drives()
        if not drives:
            if full:
                QMessageBox.warning(self, APP_NAME, "No disks or drives were found on this PC.")
            return
        self._volume_used = 0
        if full:
            # progress = bytes seen / used space of all drives
            for d in drives:
                try:
                    u = shutil.disk_usage(d)
                    self._volume_used += u.total - u.free
                except OSError:
                    pass
            self._clear_views()                # node indices are about to change
        n = len(drives)
        verb = "Scanning" if full else "Updating"
        self._set_busy(True, f"{verb} {ROOT_LABEL} — {n} drive{'s' if n != 1 else ''}")
        sig = self.sig_scan if full else self.sig_refresh
        sig.emit(drives, ROOT_LABEL, self.db_path)

    def _start_scan(self):
        """Index every drive from scratch: first run, or nothing loaded."""
        if self.busy:
            return
        self._start_job(full=True)

    def _start_refresh(self):
        """Bring the loaded index up to date against the drives. Folders whose
        timestamp has not moved are reused, so this is normally a few seconds.

        Called straight after a successful load, which is still "busy", so the
        ring simply carries on under a new title instead of flashing away —
        which is why this one does not bail out when busy."""
        self._start_job(full=False)

    def _manual_refresh(self):
        """The Refresh button, on any page: update the index against the drives,
        or scan from scratch if there is nothing loaded yet. The progress ring
        lives on the Explorer page, so jump there to show it."""
        if self.busy or self.engine is None:
            return
        self._go_to_page(0)
        if self.engine.has_data():
            self._start_refresh()
        else:
            self._start_scan()

    def _refresh_views(self, keep_place: bool = False):
        """Redraw everything from the current index: the one place that does it.
        The tree and treemap are rebuilt now; the list pages refill themselves
        the next time they are on screen.

        With keep_place, the engine has swapped in a *new* index, so every node
        index the views were holding is stale — the user is put back where they
        were by path instead."""
        root_path, sel_path = self._last_root_path, self._last_sel_path
        self._populate()
        for page in self.list_pages:
            page.invalidate()
        if not keep_place:
            return
        idx = self.engine.find_path(root_path)
        if idx >= 0:
            self.treemap.set_root(idx)
        idx = self.engine.find_path(sel_path)
        if idx >= 0:
            self._reveal_in_tree(idx)
            if not self.treemap.highlight(idx):
                self.treemap.highlight_whole(self.treemap.root)

    def _cancel(self):
        if self.engine and self.busy:
            self.engine.cancel()
            self.explorer.busy_panel.detail.setText("Cancelling…")

    @Slot(int, object, object, object, object, object, str)
    def _on_progress(self, phase, files, dirs, nbytes, done, total, path):
        panel = self.explorer.busy_panel
        if phase == PHASE_SCAN:
            frac = min(0.99, nbytes / self._volume_used) if self._volume_used > 0 else None
            panel.set_progress(frac, fmt_size(nbytes),
                               f"{files:,} files · {dirs:,} folders · {fmt_size(nbytes)}", path)
        else:
            label = {PHASE_INDEX: "Indexing", PHASE_SAVE: "Saving",
                     PHASE_LOAD: "Loading", PHASE_VERIFY: "Updating"}.get(phase, "Working")
            frac = done / total if total > 0 else None
            panel.set_progress(frac, "", f"{label} · {done:,} / {total:,} items", path)

    @Slot(str, int, str)
    def _on_finished(self, job, code, detail):
        e = self.engine
        # A successful load goes straight on to the update, so the ring stays up
        # and the user sees one continuous progress pass instead of a flash of
        # stale results.
        if job == "load" and code == DV_OK and e.has_data():
            QTimer.singleShot(0, self._start_refresh)
            return
        self._set_busy(False)                  # hides the ring, shows tree + treemap
        msg = e.error_text(code)
        if e.has_data() and job != "save":
            # A finished refresh means a brand-new index, so the views have to
            # find their way back by path; anything else (scan, load, or a
            # refresh that changed nothing) just redraws.
            self._refresh_views(keep_place=(job == "refresh" and code == DV_OK))
        if job == "load" and code == DV_ERR_NO_DATA and not e.has_data():
            QTimer.singleShot(0, self._start_scan)     # first start / old format: scan everything
            return
        if code in (DV_OK, DV_ERR_NO_CHANGE, DV_ERR_NO_DATA, DV_ERR_CANCELLED):
            return
        if code == DV_ERR_DB_CORRUPT:
            self._offer_db_reset(job, detail or msg)
        elif code == DV_ERR_NOMEM:
            QMessageBox.critical(self, APP_NAME, f"{msg}.\n\n{detail}\n\nThere are too many files to "
                                                 f"index with the memory available.")
        elif code in (DV_ERR_DB_WRITE, DV_ERR_DB_OPEN):
            extra = "\n\nThe scan results are still shown, but they will not be there next time." if job == "scan" else ""
            QMessageBox.warning(self, APP_NAME, f"{msg}.\n\n{detail}{extra}")
        else:
            QMessageBox.warning(self, APP_NAME, f"{msg}.\n\n{detail}")

    def _offer_db_reset(self, job, detail):
        has = self.engine.has_data()
        ans = QMessageBox.question(
            self, APP_NAME,
            f"The saved database is damaged or from another version:\n\n{detail}\n\n"
            f"Delete it{' and save the current results again' if has else ' and scan all drives again'}?\n"
            f"{self.db_path}")
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_db_files(self.db_path)
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Could not delete the database:\n{ex}")
            return
        if has:
            self._set_busy(True, "Saving…")
            self.sig_save.emit(self.db_path)
        else:
            self._start_scan()

    def _populate(self):
        self.model.reload()
        top = self.model.top_index()
        if top.isValid():
            self.explorer.tree.expand(top)         # "This PC" open → every drive visible
        self.treemap.set_root(0)

    # ------------------------------------------------------------------ sync
    def _on_tree_current(self, current, _previous):
        node = self.model.node_at(current)
        if node is None or self.engine is None:
            return
        self._last_sel_path = self.engine.path(node.idx)
        self._show_in_treemap(node.idx)

    def _show_in_treemap(self, idx):
        """Tree → treemap: outline the block; zoom the treemap if it is not drawn."""
        tm = self.treemap
        if tm.highlight(idx) or self._syncing:
            return
        info = self.engine.info(idx)
        if info is None:
            return
        if info.parent >= 0:
            tm.set_root(info.parent)               # show it among its siblings
            if tm.highlight(idx):
                return
        if info.is_dir:
            tm.set_root(idx)                       # too small even there: zoom into it
            tm.highlight_whole(idx)

    @Slot(int)
    def _reveal_in_tree(self, idx):
        """Treemap → tree: expand parents, select the row and scroll to it."""
        index = self.model.ensure_index(idx)
        if not index.isValid():
            return
        tree = self.explorer.tree
        self._syncing = True
        try:
            parents, p = [], index.parent()
            while p.isValid():
                parents.append(p)
                p = p.parent()
            for p in reversed(parents):
                tree.expand(p)
            tree.setCurrentIndex(index)
            tree.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        finally:
            self._syncing = False

    @Slot(int)
    def _on_treemap_root(self, idx):
        if idx < 0 or self.engine is None:
            return
        info = self.engine.info(idx)
        self._last_root_path = self.engine.path(idx)
        self.explorer.btn_up.setEnabled(bool(info and info.parent >= 0))
        kind = self.engine.kind(idx, info)
        where = self.engine.label(idx, info) if kind == KIND_DRIVE else self.engine.path(idx)
        text = f"{where}    {fmt_size(info.size) if info else ''}"
        fm = self.explorer.crumb.fontMetrics()
        self.explorer.crumb.setText(fm.elidedText(text, Qt.TextElideMode.ElideMiddle,
                                                  max(self.explorer.crumb.width(), 300)))
        self.explorer.crumb.setToolTip(text)

    def _tree_menu(self, pos):
        tree = self.explorer.tree
        node = self.model.node_at(tree.indexAt(pos))
        if node is None:
            return
        real = node.kind != KIND_PC                # "This PC" is not a folder on disk
        path = self.engine.path(node.idx) if real else ""
        menu = QMenu(self)
        act_open = menu.addAction("Open")
        act_show = menu.addAction("Show in file manager")
        act_copy = menu.addAction("Copy path")
        for a in (act_open, act_show, act_copy):
            a.setEnabled(real)
        menu.addSeparator()
        act_zoom = menu.addAction("Zoom treemap here")
        act_zoom.setEnabled(node.is_dir and node.child_count > 0)
        act = menu.exec(tree.viewport().mapToGlobal(pos))
        if act == act_open:
            open_path(path)
        elif act == act_show:
            show_in_file_manager(path, node.is_dir)
        elif act == act_copy:
            QGuiApplication.clipboard().setText(path)
        elif act == act_zoom:
            self.treemap.set_root(node.idx)
            self.treemap.highlight_whole(node.idx)

    def _on_page(self, i):
        self.stack.setCurrentIndex(i)

    def _go_to_page(self, i):
        """Switch pages from code, keeping the menu bar's highlight in step."""
        btn = self.menubar.group.button(i)
        if btn is not None:
            btn.setChecked(True)
        self._on_page(i)

    # ------------------------------------------------------------------ shutdown
    def closeEvent(self, event):
        if self.thread is not None:
            if self.busy:
                self.engine.cancel()
            self.thread.quit()
            self.thread.wait(15000)
        super().closeEvent(event)

    # ---- maximise / restore
    def toggle_max_restore(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "titlebar"):
            maximized = self.isMaximized()
            m = 0 if maximized else RESIZE_MARGIN
            self._layout.setContentsMargins(m, m, m, m)
            self.titlebar.btn_max.set_kind("restore" if maximized else "max")
        super().changeEvent(event)

    # ---- edge resizing for the frameless window
    def _edges_at(self, pos):
        m = RESIZE_MARGIN
        edges = Qt.Edge(0)
        if pos.x() < m:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= self.width() - m:
            edges |= Qt.Edge.RightEdge
        if pos.y() < m:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= self.height() - m:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _update_cursor(self, edges):
        l = bool(edges & Qt.Edge.LeftEdge)
        r = bool(edges & Qt.Edge.RightEdge)
        t = bool(edges & Qt.Edge.TopEdge)
        b = bool(edges & Qt.Edge.BottomEdge)
        if (l and t) or (r and b):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif (r and t) or (l and b):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif l or r:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif t or b:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self.isMaximized():
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self._update_cursor(self._edges_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edges = self._edges_at(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)


def main():
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiskVisualizer.App")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI", 9))

    try:
        engine, error = DiskEngine(), None
    except Exception as e:                      # missing / stale / unloadable DLL
        engine, error = None, str(e)
    window = MainWindow(engine, error)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
