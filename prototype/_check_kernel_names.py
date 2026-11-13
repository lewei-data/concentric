#!/usr/bin/env python3
"""kernel.py 未定义名静态检查（拆分轮 B 配套）。"""
import ast, os, builtins

BASE = "/home/boray/opc-workbench/prototype"
kcode = open(os.path.join(BASE, "kernel.py"), encoding="utf-8").read()
ktree = ast.parse(kcode)

defined = set(dir(builtins))
for node in ktree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for al in node.names:
            defined.add((al.asname or al.name).split(".")[0])
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    defined.add(sub.id)

for node in ast.walk(ktree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        defined.add(node.name)
        for a in node.args.args + node.args.kwonlyargs:
            defined.add(a.arg)
        if node.args.vararg:
            defined.add(node.args.vararg.arg)
        if node.args.kwarg:
            defined.add(node.args.kwarg.arg)
    elif isinstance(node, ast.ClassDef):
        defined.add(node.name)
    elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        defined.add(node.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for al in node.names:
            defined.add((al.asname or al.name).split(".")[0])
    elif isinstance(node, ast.ExceptHandler) and node.name:
        defined.add(node.name)
    elif isinstance(node, ast.comprehension):
        for t in ast.walk(node.target):
            if isinstance(t, ast.Name):
                defined.add(t.id)
    elif isinstance(node, ast.Global):
        defined.update(node.names)

loaded = set()
for node in ast.walk(ktree):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        loaded.add(node.id)
    elif isinstance(node, ast.Attribute):
        v = node.value
        while isinstance(v, ast.Attribute):
            v = v.value
        if isinstance(v, ast.Name) and isinstance(v.ctx, ast.Load):
            loaded.add(v.id)

undef = sorted(loaded - defined)
print("kernel.py 疑似未定义名:", undef if undef else "无")
