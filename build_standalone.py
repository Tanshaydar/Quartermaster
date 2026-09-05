import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
APP_OUT_DIR = os.path.join(DIST_DIR, "Quartermaster")
IS_WIN = sys.platform.startswith("win")
PLATFORM_TAG = "windows-x64" if IS_WIN else "linux-x64"

print(f"=== Building Standalone Quartermaster Distribution ({PLATFORM_TAG}) ===")

# Clean previous build artifacts for Quartermaster only
shutil.rmtree(APP_OUT_DIR, ignore_errors=True)
shutil.rmtree(os.path.join(BUILD_DIR, "Quartermaster"), ignore_errors=True)
os.makedirs(APP_OUT_DIR, exist_ok=True)

# Auto-generate multi-binary spec file for GUI and MCP server
spec_path = os.path.join(BASE_DIR, "Quartermaster.spec")
spec_content = f"""# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None
BASE_DIR = r"{BASE_DIR}"

COMMON_EXCLUDES = [
    'torch', 'torchvision', 'torchaudio',
    'cv2', 'scipy', 'sklearn', 'pandas',
    'llvmlite', 'numba', 'matplotlib',
    'transformers', 'IPython', 'jupyter',
    'tensorboard', 'tkinter', 'unittest'
]

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
    excludes=COMMON_EXCLUDES,
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
    excludes=COMMON_EXCLUDES,
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
    icon=os.path.join(BASE_DIR, 'assets', 'icon.ico' if os.name == 'nt' else 'icon.png'),
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
    icon=os.path.join(BASE_DIR, 'assets', 'icon.ico' if os.name == 'nt' else 'icon.png'),
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

# Package archive and calculate hash
if IS_WIN:
    archive_path = os.path.join(DIST_DIR, f"Quartermaster-{PLATFORM_TAG}.zip")
    if os.path.exists(archive_path):
        os.remove(archive_path)
    print(f"Creating release archive: {archive_path}...")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(APP_OUT_DIR):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, APP_OUT_DIR)
                zf.write(full_path, rel_path)
else:
    archive_path = os.path.join(DIST_DIR, f"Quartermaster-{PLATFORM_TAG}.tar.gz")
    if os.path.exists(archive_path):
        os.remove(archive_path)
    print(f"Creating release archive: {archive_path}...")
    with tarfile.open(archive_path, "w:gz") as tf:
        for root, _, files in os.walk(APP_OUT_DIR):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, APP_OUT_DIR)
                tf.add(full_path, arcname=rel_path)

h = hashlib.sha256()
with open(archive_path, "rb") as f:
    while chunk := f.read(65536):
        h.update(chunk)
sha256_hash = h.hexdigest()

print(f"\n=== Build SUCCESSFUL! ===")
print(f"Output folder: {APP_OUT_DIR}")
print(f"Release archive: {archive_path}")
print(f"SHA-256: {sha256_hash}")
