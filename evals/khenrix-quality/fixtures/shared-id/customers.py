from id_utils import canonical_id


def find_customer(customers, customer_id):
    return customers[canonical_id(customer_id)]
