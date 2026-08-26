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

# PyInstaller command
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--onedir",
    "--windowed",
    "--name=Quartermaster",
    f"--icon={os.path.join(BASE_DIR, 'assets', 'icon.ico')}",
    f"--add-data={os.path.join(BASE_DIR, 'assets')}{os.pathsep}assets",
    f"--add-data={os.path.join(BASE_DIR, 'web')}{os.pathsep}web",
    f"--add-data={os.path.join(BASE_DIR, 'data', 'recipes.json')}{os.pathsep}data",
    f"--add-data={os.path.join(BASE_DIR, 'data', 'concepts.json')}{os.pathsep}data",
    "--hidden-import=PySide6.QtNetwork",
    "--hidden-import=fastembed",
    "--hidden-import=onnxruntime",
    "--hidden-import=PIL",
    "--hidden-import=sqlite3",
    "--hidden-import=httpx",
    os.path.join(BASE_DIR, "src", "desktop.py")
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
