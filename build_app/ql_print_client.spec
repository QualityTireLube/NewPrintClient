# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

VENV        = os.path.expanduser('~/ql-print-client/venv')
SITE_PKGS   = os.path.join(VENV, 'lib/python3.9/site-packages')
WEBVIEW_DIR = os.path.join(SITE_PKGS, 'webview')

# Collect all webview data files (js/, platforms/, etc.)
webview_datas = collect_data_files('webview')

a = Analysis(
    ['../launcher.py'],
    pathex=['..'],
    binaries=[],
    datas=webview_datas,
    hiddenimports=[
        # Flask / Werkzeug
        'flask', 'flask.templating',
        'jinja2', 'jinja2.ext',
        'werkzeug', 'werkzeug.serving', 'werkzeug.routing',
        # Requests / urllib3
        'requests', 'urllib3', 'certifi', 'charset_normalizer', 'idna',
        # pypdf
        'pypdf', 'pypdf._crypt_filters', 'pypdf.filters',
        # pywebview + all submodules
        *collect_submodules('webview'),
        # PyObjC frameworks needed by pywebview on macOS
        'objc',
        *collect_submodules('AppKit'),
        *collect_submodules('Foundation'),
        *collect_submodules('WebKit'),
        *collect_submodules('Cocoa'),
        # misc
        'pkg_resources.py2_warn',
    ],
    # Use pywebview's own PyInstaller hook
    hookspath=[os.path.join(WEBVIEW_DIR, '__pyinstaller')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL', 'PyQt5', 'wx', 'test'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='QL Print Client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='QL Print Client',
)

app = BUNDLE(
    coll,
    name='QL Print Client.app',
    icon='AppIcon.icns',
    bundle_identifier='com.qualitytire.ql-print-client',
    version='2.0.0',
    info_plist={
        'CFBundleName':            'QL Print Client',
        'CFBundleDisplayName':     'QL Print Client',
        'CFBundleShortVersionString': '2.0',
        'CFBundleVersion':         '2.0.0',
        'LSMinimumSystemVersion':  '11.0',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass':        'NSApplication',
        'NSAppTransportSecurity':  {'NSAllowsArbitraryLoads': True},
    },
)
