"""Static regression test: every name used in an annotation must be imported.

Python 3.14 defers annotation evaluation (PEP 649), so missing typing imports
run fine locally but raise NameError at import time on <= 3.13. This check is
static, so it catches those on every interpreter — including in CI.
"""
import ast
import os
import unittest

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
TYPING_NAMES = {"Optional", "List", "Dict", "Any", "Tuple", "Union", "Callable", "Set"}


def _annotation_names(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.AnnAssign) and n.annotation:
            yield from (x.id for x in ast.walk(n.annotation) if isinstance(x, ast.Name))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            anns = [x.annotation for x in [*a.args, *a.posonlyargs, *a.kwonlyargs] if x.annotation]
            if a.vararg is not None:
                anns.append(a.vararg.annotation)
            if a.kwarg is not None:
                anns.append(a.kwarg.annotation)
            if n.returns is not None:
                anns.append(n.returns)
            for ann in anns:
                if ann is not None:
                    yield from (x.id for x in ast.walk(ann) if isinstance(x, ast.Name))


class TestAnnotationImports(unittest.TestCase):
    def test_all_annotation_names_are_imported(self):
        problems = []
        for fname in sorted(os.listdir(SRC_DIR)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(SRC_DIR, fname)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fname)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "typing":
                    imported |= {a.name for a in node.names}
            used = set(_annotation_names(tree))
            missing = {u for u in used if u in TYPING_NAMES and u not in imported}
            if missing:
                problems.append(f"{fname}: annotation names {sorted(missing)} used but not imported from typing")
        self.assertEqual(problems, [], "missing typing imports:\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
