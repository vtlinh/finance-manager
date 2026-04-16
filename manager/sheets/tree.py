from collections import defaultdict


def _build_tree(groups: list[dict]) -> tuple[dict, list[str]]:
    """Build a nested category tree from grouped transactions.

    Returns (root, months) where root is a nested dict with keys:
      - amounts: {month: total_amount}
      - children: {category_name: child_node}
      - merchants: {month: [(name, amount), ...]}
    """
    months = sorted({g["month"] for g in groups})

    def new_node() -> dict:
        return {"amounts": defaultdict(float), "children": {}, "merchants": defaultdict(list)}

    root = new_node()
    for g in groups:
        path = g.get("path", ["Uncategorized"])
        node = root
        node["amounts"][g["month"]] += g["amount"]
        node["merchants"][g["month"]].append((g["name"], g["amount"]))
        for part in path:
            node["children"].setdefault(part, new_node())
            node = node["children"][part]
            node["amounts"][g["month"]] += g["amount"]
            node["merchants"][g["month"]].append((g["name"], g["amount"]))

    return root, months
