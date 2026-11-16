import ast

from .effect_detector import classify_call, dotted_name


class FunctionVisitor(ast.NodeVisitor):
    def __init__(self):
        self.calls = []
        self.branches = []
        self.exceptions = []
        self.assignments = []

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        pass

    def visit_Lambda(self, node):
        pass

    def visit_Call(self, node):
        self.generic_visit(node)
        self.calls.append({"name": dotted_name(node.func), "line": node.lineno,
                           "column": node.col_offset, "kind": classify_call(node)})

    def visit_If(self, node):
        self.branches.append({"line": node.lineno, "condition": ast.unparse(node.test)})
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        self.exceptions.append({"line": node.lineno,
                                "type": ast.unparse(node.type) if node.type else "BaseException"})
        self.generic_visit(node)

    def visit_Assign(self, node):
        self.assignments.append({"line": node.lineno,
                                 "targets": [ast.unparse(target) for target in node.targets]})
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.assignments.append({"line": node.lineno, "targets": [ast.unparse(node.target)]})
        self.generic_visit(node)


def parse_source(source, path="<source>"):
    tree = ast.parse(source, filename=path)
    functions = []

    def walk(node, scope=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visitor = FunctionVisitor()
                for statement in child.body:
                    visitor.visit(statement)
                routes = []
                for decorator in child.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    name = dotted_name(decorator.func)
                    method = name.rsplit(".", 1)[-1].upper()
                    route_path = decorator.args[0] if decorator.args else next(
                        (kw.value for kw in decorator.keywords if kw.arg == "path"), None)
                    if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} and isinstance(route_path, ast.Constant) and isinstance(route_path.value, str):
                        routes.append({"method": method, "path": route_path.value,
                                       "line": decorator.lineno})
                start = min([child.lineno] + [item.lineno for item in child.decorator_list])
                functions.append({"path": path, "name": child.name,
                                  "qualified_name": ".".join((*scope, child.name)),
                                  "line": child.lineno, "start_line": start, "end_line": child.end_lineno,
                                  "async": isinstance(child, ast.AsyncFunctionDef),
                                  "decorators": [ast.unparse(item) for item in child.decorator_list],
                                  "routes": routes, "calls": visitor.calls,
                                  "branches": visitor.branches, "exceptions": visitor.exceptions,
                                  "assignments": visitor.assignments})
                walk(child, (*scope, child.name))
            elif isinstance(child, ast.ClassDef):
                walk(child, (*scope, child.name))
            else:
                walk(child, scope)

    walk(tree)
    return functions
