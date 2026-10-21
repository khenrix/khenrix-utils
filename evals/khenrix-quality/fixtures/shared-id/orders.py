from id_utils import canonical_id


def import_order(payload, customers):
    customer_id = canonical_id(payload["customer_id"])
    if not isinstance(payload["quantity"], int) or payload["quantity"] <= 0:
        raise ValueError("quantity must be a positive integer")
    customer = customers[customer_id]
    return {"customer_id": customer_id, "customer_name": customer["name"],
            "quantity": payload["quantity"]}
