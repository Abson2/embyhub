#!/usr/bin/env python3
"""
favicon 契约：所有自带 <head> 的页面都显式声明带版本串的新图标；
回退路由 /favicon.ico 不缓存；模板文件不带 BOM。

用法: python tools/check_favicon_contract.py [ROOT]   (ROOT 默认当前目录)
退出码: 0 = 通过, 1 = 命中
"""
from __future__ import annotations

import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
ICON = "/static/img/favicon.png?v=20261108"
SKIP_DIRS = {".git", "app/data", "node_modules", "__pycache__", ".venv"}


def read_bytes(rel: str) -> bytes:
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


def read_text(rel: str) -> str:
    return read_bytes(rel).decode("utf-8")


def html_files(base: str) -> list[str]:
    out = []
    for dirpath, dirs, files in os.walk(os.path.join(ROOT, base)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn.endswith(".html"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), ROOT))
    return out


def main() -> int:
    fails: list[str] = []

    # 1) 模板不能有 BOM：BOM 会输出在 DOCTYPE 之前，触发浏览器怪异解析
    for base in ("templates", "app/templates", "app/plugins"):
        for rel in html_files(base):
            if read_bytes(rel).startswith(b"\xef\xbb\xbf"):
                fails.append(f"{rel}: 文件开头存在 UTF-8 BOM")

    # 2) 凡是自带 <head> 且不继承 layout 的模板，必须自己声明 favicon
    for rel in html_files("templates"):
        s = read_text(rel)
        if "<head" in s and 'extends "layout.html"' not in s:
            if 'rel="icon"' not in s or ICON not in s:
                fails.append(f"{rel}: 独立 HTML 缺少带版本串的 favicon 声明")

    # 3) layout 必须声明，且所有 rel=icon 统一指向同一张图
    if ICON not in read_text("templates/layout.html"):
        fails.append("templates/layout.html: 缺少带版本串的 favicon 声明")
    for rel in html_files("templates"):
        for m in re.finditer(r'<link[^>]*rel="icon"[^>]*>', read_text(rel)):
            if ICON not in m.group(0):
                fails.append(f"{rel}: rel=icon 未指向统一图标 {ICON}")

    # 4) /favicon.ico 回退路由：返回新图且不缓存
    views = read_text("app/domains/system/views.py")
    m = re.search(
        r'@router\.get\("/favicon\.ico"\)[\s\S]{0,600}?'
        r'return FileResponse\([\s\S]{0,300}?\)',
        views,
    )
    if not m or "favicon.png" not in m.group(0) or "no-cache" not in m.group(0):
        fails.append(
            "app/domains/system/views.py: /favicon.ico 需返回 favicon.png 且带 no-cache 头"
        )

    # 5) 后端内联返回的完整 HTML 页面同样要声明
    for block in re.finditer(r"<!DOCTYPE html>[\s\S]*?</head>", views):
        if 'rel="icon"' not in block.group(0):
            fails.append("app/domains/system/views.py: 内联 HTML 页面缺少 favicon 声明")

    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("favicon contract OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
