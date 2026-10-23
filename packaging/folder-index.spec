# PyInstaller spec: one folder holding two executables that share the same runtime.
#   FolderIndex.exe   - the desktop app (window + tray), no console
#   folder-index.exe  - the CLI and MCP server (stdio), console
# Build:  uv run pyinstaller packaging/folder-index.spec --noconfirm
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
ICON = str(ROOT / "packaging" / "folder-index.ico")

hidden = (
    collect_submodules("folder_index")
    + collect_submodules("winrt")
    # mcp.cli needs typer (the `mcp` dev CLI), which we neither ship nor use
    + collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))
    + ["pypdfium2", "pypdfium2_raw", "clr", "pystray._win32", "webview.platforms.edgechromium",
       "webview.platforms.winforms"]
)
datas = [(str(SRC / "folder_index" / "ui"), "folder_index/ui")]
datas += collect_data_files("webview") + collect_data_files("pypdfium2_raw") + collect_data_files("pypdfium2")

common = dict(pathex=[str(SRC)], hiddenimports=hidden, datas=datas, excludes=["tkinter", "pytest"])

app_a = Analysis([str(ROOT / "packaging" / "entry_app.py")], **common)
cli_a = Analysis([str(ROOT / "packaging" / "entry_cli.py")], **common)

app_pyz = PYZ(app_a.pure)
cli_pyz = PYZ(cli_a.pure)

app_exe = EXE(app_pyz, app_a.scripts, [], exclude_binaries=True, name="FolderIndex", icon=ICON,
              console=False, version=str(ROOT / "packaging" / "version.txt"))
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="folder-index", icon=ICON,
              console=True, version=str(ROOT / "packaging" / "version.txt"))

coll = COLLECT(app_exe, app_a.binaries, app_a.datas, cli_exe, cli_a.binaries, cli_a.datas,
               name="FolderIndex")
