import ast
import sys

source = sys.stdin.read()
tree = ast.parse(source)
functions = {
    node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
}


def splits_csv(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "split"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == ","
    )


def nesting(node):
    level = int(isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)))
    return level + max((nesting(child) for child in ast.iter_child_nodes(node)), default=0)


csv_functions = [
    name for name, function in functions.items()
    if any(splits_csv(node) for node in ast.walk(function))
]
method_checks = sum(
    isinstance(node, ast.Compare)
    and isinstance(node.left, ast.Name)
    and node.left.id == "method"
    for node in ast.walk(tree)
)

print("Physical lines:", len(source.splitlines()))
print("Functions that split CSV:", len(csv_functions))
print("Direct method comparisons:", method_checks)

date_functions = sys.argv[1:] or ["date_filter", "string_date_filter"]
for name in date_functions:
    print(f"{name} nesting:", nesting(functions[name]))