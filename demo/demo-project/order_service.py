"""订单结算服务（示例，故意留了 4 个缺陷）"""

from dataclasses import dataclass, field


@dataclass
class Order:
    items: list = field(default_factory=list)
    total: float = 0.0


def average_price(items):
    total = sum(it["price"] for it in items)
    return total / len(items)


def first_item_name(order):
    return order.items[0]["name"]


def add_item(order, name, price, qty):
    order.items.append({"name": name, "price": price, "qty": qty})
    order.total += price * qty
    return order


def is_free(order):
    return order.total == 0.0


def settle(order):
    name = first_item_name(order)
    avg = average_price(order.items)
    return {"first": name, "avg": avg, "free": is_free(order)}


def refund(order):
    return order.total * -1
