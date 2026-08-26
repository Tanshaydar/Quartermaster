import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
APP_OUT_DIR = os.path.join(DIST_DIR, "Quartermaster")

print("=== Building Standalone Quartermaster Distribution ===")

# Clean previous build artifacts
shutil.rmtree(DIST_DIR, ignore_errors=True)
shutil.rmtree(BUILD_DIR, ignore_errors=True)
os.makedirs(APP_OUT_DIR, exist_ok=True)

# Auto-generate multi-binary spec file for GUI and MCP server
spec_path = os.path.join(BASE_DIR, "Quartermaster.spec")
spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None
BASE_DIR = r"{BASE_DIR}"

a_gui = Analysis(
    [os.path.join(BASE_DIR, 'run_app.py')],
    pathex=[os.path.join(BASE_DIR, 'src'), BASE_DIR],
    binaries=[],
    datas=[
        (os.path.join(BASE_DIR, 'assets'), 'assets'),
        (os.path.join(BASE_DIR, 'web'), 'web'),
        (os.path.join(BASE_DIR, 'data', 'recipes.json'), 'data'),
        (os.path.join(BASE_DIR, 'data', 'concepts.json'), 'data'),
    ],
    hiddenimports=[
        'PySide6.QtNetwork', 'fastembed', 'onnxruntime', 'PIL', 'sqlite3', 'httpx',
        'src', 'src.db', 'src.config', 'src.desktop', 'src.store_client',
        'src.local_scan', 'src.vision', 'src.semantic', 'src.unpacker', 'src.stack_rules', 'src.mcp_server', 'src.register'
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a_mcp = Analysis(
    [os.path.join(BASE_DIR, 'run_mcp.py')],
    pathex=[os.path.join(BASE_DIR, 'src'), BASE_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[
        'fastembed', 'onnxruntime', 'PIL', 'sqlite3', 'httpx',
        'src', 'src.db', 'src.config', 'src.store_client',
        'src.local_scan', 'src.vision', 'src.semantic', 'src.unpacker', 'src.stack_rules', 'src.mcp_server', 'src.register'
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)
pyz_mcp = PYZ(a_mcp.pure, a_mcp.zipped_data, cipher=block_cipher)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name='Quartermaster',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(BASE_DIR, 'assets', 'icon.ico'),
)

exe_mcp = EXE(
    pyz_mcp,
    a_mcp.scripts,
    [],
    exclude_binaries=True,
    name='Quartermaster-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(BASE_DIR, 'assets', 'icon.ico'),
)

coll = COLLECT(
    exe_gui,
    exe_mcp,
    a_gui.binaries + a_mcp.binaries,
    a_gui.zipfiles + a_mcp.zipfiles,
    a_gui.datas + a_mcp.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Quartermaster',
)
"""

with open(spec_path, "w", encoding="utf-8") as f:
    f.write(spec_content)

# PyInstaller command using multi-binary spec
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    spec_path
]

print("Running command:", " ".join(cmd))
res = subprocess.run(cmd, cwd=BASE_DIR)
if res.returncode != 0:
    print("Build FAILED with return code:", res.returncode)
    sys.exit(res.returncode)

# Copy additional supporting assets and readme
for item in ["README.md", "LICENSE"]:
    src_path = os.path.join(BASE_DIR, item)
    if os.path.exists(src_path):
        shutil.copy2(src_path, APP_OUT_DIR)

print(f"\n=== Build SUCCESSFUL! Output location: {APP_OUT_DIR} ===")
